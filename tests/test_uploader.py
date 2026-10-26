"""测试 net/uploader.py：身份、要传哪些天、重试、失败后水位线不动。

和其它测试一样，这里用临时库，不碰你本机那份 usage.db。

**测试里不发真实网络请求**：所有用例都把 uploader._post_json 换成一个假的，
这样测试不依赖真实服务端是否可达，也不会往真服务端灌垃圾数据。
真实那条链路（造数据 → 上传 → 用服务端接口读回来）是另外手工验的。
"""
import os
import tempfile
import unittest
import uuid
from datetime import date, timedelta
from unittest import mock

from core import config, storage
from net import uploader


class UploaderTestCase(unittest.TestCase):
    """公共脚手架：临时数据库 + 把 HTTP 换掉。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.original_db_path = storage.DB_PATH
        self.addCleanup(setattr, storage, "DB_PATH", self.original_db_path)
        storage.DB_PATH = os.path.join(self.temp_dir.name, "usage.db")
        storage.init_db()

        # 重试的退避 sleep 全部吃掉，否则失败用例要真等 2+4+8 秒
        patcher = mock.patch.object(uploader.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

        # 默认假装上传成功。需要模拟失败的用例自己再换掉。
        self.requests = []          # 记录每次发出去的 payload
        self.fake = mock.Mock(side_effect=self._fake_ok)
        self.original_post = uploader._post_json
        self.addCleanup(setattr, uploader, "_post_json", self.original_post)
        uploader._post_json = self.fake

    def _fake_ok(self, url, payload, timeout):
        """一个假的 _post_json，两个接口共用。

        上传和订阅走的是同一个 _post_json，但请求体形状完全不同：
        上传有 user_key/days，订阅只有 email/subscribed。所以这里按
        「有没有 user_key」分流，免得订阅那些用例去读一个不存在的键。
        """
        self.requests.append(payload)
        if "user_key" not in payload:
            # 订阅接口 PUT /subscription/{user_key}
            return {"ok": True, "email": payload.get("email"),
                    "subscribed": payload.get("subscribed", True)}
        return {
            "ok": True,
            "user_key": payload["user_key"],
            "days_written": len(payload["days"]),
            "apps_written": sum(len(d["apps"]) for d in payload["days"]),
            "email_saved": bool(payload.get("email")),
        }

    # ── 造数据 ──

    def _add_usage(self, day, app="example.exe", seconds=600.0, launches=1):
        """往临时库里塞一条某天的使用记录。

        storage 记录的时长是按 起止时间 算的，所以这里让 start/end 直接
        对得上 seconds，避免测试数据和断言对不上。
        """
        for i in range(launches):
            start_min = 10 * 60 + i * 30
            end_min = start_min + int(seconds // launches // 60) or 1
            storage.record_fragment(
                app, "Example", f"C:/Example/{app}",
                "%sT%02d:%02d:00" % (day, start_min // 60, start_min % 60),
                "%sT%02d:%02d:00" % (day, end_min // 60, end_min % 60),
                seconds / launches,
            )

    # ── 身份 ──

    def test_user_key_is_stable_uuid(self):
        """首次调用生成 UUID，之后每次拿到的都是同一个。"""
        k1 = uploader.get_or_create_user_key()
        k2 = uploader.get_or_create_user_key()
        self.assertEqual(k1, k2)
        uuid.UUID(k1)   # 不是合法 UUID 会抛异常，测试就算失败

    def test_reset_user_key_clears_watermark(self):
        """换身份时必须把水位线一起清掉。

        否则新 key 在服务端一天数据都没有，而水位线还指着「传到了 10-15」，
        历史数据就永远补不上去了。
        """
        old = uploader.get_or_create_user_key()
        storage.set_setting("upload_watermark", "2026-10-15")
        new = uploader.reset_user_key()
        self.assertNotEqual(old, new)
        self.assertEqual(storage.get_setting("upload_watermark"), "")

    def test_email_empty_means_none(self):
        """没填邮箱时传 None，而不是空字符串 —— 服务端的字段是可空的。"""
        self.assertIsNone(uploader.get_email())
        storage.set_setting("email", "  ")
        self.assertIsNone(uploader.get_email())
        storage.set_setting("email", "a@b.com")
        self.assertEqual(uploader.get_email(), "a@b.com")

    # ── 要传哪些天 ──

    def test_first_run_pending_is_all_days(self):
        """从没传过时，本地有数据的天全都要传（补历史）。"""
        self._add_usage("2026-10-12")
        self._add_usage("2026-10-14")
        pending = uploader.pending_days(today=date(2026, 10, 14))
        self.assertEqual([d.isoformat() for d in pending],
                         ["2026-10-12", "2026-10-14"])

    def test_pending_excludes_days_without_data(self):
        """本地没数据的天不传：服务端把「有这天但零个应用」当成异常跳过，
        传过去只会白白多一条日志。"""
        self._add_usage("2026-10-14")
        pending = uploader.pending_days(today=date(2026, 10, 20))
        self.assertEqual([d.isoformat() for d in pending], ["2026-10-14"])

    def test_today_is_resent_even_after_uploading(self):
        """今天要一直重传 —— 这是水位线机制里最关键的一条。

        今天的时长还在长，如果传完就把今天划走，那今天剩下的几个小时
        再也不会被上传。所以「最近几天」永远留在待传列表里。
        """
        self._add_usage("2026-10-14")
        storage.set_setting("upload_watermark", "2026-10-14")
        pending = uploader.pending_days(today=date(2026, 10, 14))
        self.assertIn("2026-10-14", [d.isoformat() for d in pending])

    def test_old_days_outside_resend_window_are_not_resent(self):
        """超出「回看窗口」的老日子不再重传，否则每跑一次都要把历史全发一遍。"""
        self._add_usage("2026-01-01")
        self._add_usage("2026-10-14")
        storage.set_setting("upload_watermark", "2026-10-14")
        pending = uploader.pending_days(today=date(2026, 10, 14))
        self.assertNotIn("2026-01-01", [d.isoformat() for d in pending])

    def test_gap_days_are_backfilled(self):
        """中间几天没开机，下次启动要把空档补上（水位线之后的都算新）。"""
        self._add_usage("2026-10-12")
        storage.set_setting("upload_watermark", "2026-10-12")
        self._add_usage("2026-10-13")
        pending = uploader.pending_days(today=date(2026, 10, 14))
        self.assertIn("2026-10-13", [d.isoformat() for d in pending])

    def test_broken_watermark_falls_back_to_full_backfill(self):
        """水位线被写坏时不应该崩，退回「全量补传」。"""
        self._add_usage("2026-10-14")
        storage.set_setting("upload_watermark", "这不是日期")
        pending = uploader.pending_days(today=date(2026, 10, 14))
        self.assertEqual([d.isoformat() for d in pending], ["2026-10-14"])

    # ── 请求体 ──

    def test_payload_shape_matches_server_contract(self):
        """请求体的字段名必须和服务端的 Pydantic 模型逐字对应。

        这里的断言看着啰嗦，但它挡的是「改名忘了同步」这类最难查的 bug ——
        真出问题时表现只是服务端 422，客户端这边看不出哪里错。
        """
        self._add_usage("2026-10-14", app="pycharm64.exe", seconds=7200.0,
                        launches=2)
        uploader.upload_pending(today=date(2026, 10, 14))

        self.assertEqual(len(self.requests), 1)
        body = self.requests[0]
        self.assertEqual(
            set(body),
            {"user_key", "email", "hostname", "platform", "client_version", "days"})
        self.assertEqual(set(body["days"][0]), {"day", "apps"})
        self.assertEqual(
            set(body["days"][0]["apps"][0]),
            {"app_name", "display_name", "seconds", "launches"})
        app = body["days"][0]["apps"][0]
        self.assertEqual(app["app_name"], "pycharm64.exe")
        self.assertEqual(app["launches"], 2)
        self.assertIsInstance(app["seconds"], float)

    def test_payload_only_contains_requested_days(self):
        """只带这次要传的那几天。

        get_summary_range 是「按区间查」，区间里其它有数据的天也会被它带出来；
        如果不筛掉，传的内容和「这次传了几天」的日志就对不上，排查时误导人。
        """
        self._add_usage("2026-10-13")
        self._add_usage("2026-10-14")
        days = [date(2026, 10, 14)]
        built = uploader._build_days(days)
        self.assertEqual([b["day"] for b in built], ["2026-10-14"])

    def test_seconds_are_rounded_to_one_decimal(self):
        """秒数保留一位小数，别把浮点噪声也发过去。"""
        self._add_usage("2026-10-14", seconds=600.0)
        built = uploader._build_days([date(2026, 10, 14)])
        secs = built[0]["apps"][0]["seconds"]
        self.assertEqual(secs, round(secs, 1))

    def test_long_history_is_split_into_several_requests(self):
        """历史很长时按 MAX_DAYS_PER_REQUEST 分批，不能一个请求塞几百天。"""
        start = date(2026, 1, 1)
        # 造 40 天 > 每批 31 天，应该拆成 2 个请求
        for i in range(40):
            self._add_usage((start + timedelta(days=i)).isoformat())

        result = uploader.upload_pending(today=start + timedelta(days=39))
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["days"], 40)   # 两天批合起来的写入天数

    # ── 上传结果 ──

    def test_success_advances_watermark_and_records_time(self):
        """成功后水位线推到「这次传过的最晚一天」，并记下上传时间。"""
        self._add_usage("2026-10-13")
        self._add_usage("2026-10-14")
        result = uploader.upload_pending(today=date(2026, 10, 14))
        self.assertTrue(result["ok"])
        self.assertEqual(storage.get_setting("upload_watermark"), "2026-10-14")
        self.assertNotEqual(uploader.last_success_text(), "从未上传")

    def test_disabled_switch_sends_nothing(self):
        """关掉自动上传后，后台定时器不该产生任何网络请求。"""
        self._add_usage("2026-10-14")
        storage.set_setting("upload_enabled", "0")
        result = uploader.upload_pending(today=date(2026, 10, 14))
        self.assertEqual(result["reason"], "disabled")
        self.assertEqual(self.requests, [])

    def test_manual_upload_ignores_disabled_switch(self):
        """用户主动点「立即上传」时要能传 —— 不该被「自动上传已关闭」挡住。"""
        self._add_usage("2026-10-14")
        storage.set_setting("upload_enabled", "0")
        result = uploader.upload_pending(today=date(2026, 10, 14), force=True)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.requests), 1)

    def test_no_data_yet(self):
        """库里完全没数据时报 no_data（界面要据此提示，而不是说「没有新数据」）。"""
        result = uploader.upload_pending(today=date(2026, 10, 14))
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "no_data")

    def test_failure_keeps_watermark_untouched(self):
        """上传失败时水位线不能动，否则那几天就永远传不上去了。"""
        self._add_usage("2026-10-14")
        self.fake.side_effect = uploader.UploadError("连不上服务端：测试")
        result = uploader.upload_pending(today=date(2026, 10, 14))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "error")
        self.assertIn("连不上服务端", result["error"])
        # 从没成功过，所以这个键压根没被写过 —— None 和 "" 都算「没推进」
        self.assertIn(storage.get_setting("upload_watermark"), (None, ""))

    def test_failure_never_raises(self):
        """失败只能靠返回值表达，绝不能往外抛 —— 调用它的是后台线程。"""
        self._add_usage("2026-10-14")

        def boom(url, payload, timeout):
            raise RuntimeError("模拟内部炸了")

        uploader._post_json = boom
        result = uploader.upload_pending(today=date(2026, 10, 14))
        self.assertFalse(result["ok"])
        self.assertIn("模拟内部炸了", result["error"])

    # ── 重试 ──

    def test_transient_failure_is_retried(self):
        """网络抖动要重试，最后成功的话水位线照常推进。"""
        self._add_usage("2026-10-14")
        calls = {"n": 0}

        def flaky(url, payload, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise uploader.UploadError("连不上服务端（x:1）：测试")
            return self._fake_ok(url, payload, timeout)

        uploader._post_json = flaky
        result = uploader.upload_pending(today=date(2026, 10, 14))
        self.assertTrue(result["ok"])
        self.assertEqual(calls["n"], 2)

    def test_client_error_is_not_retried(self):
        """4xx 是「发的东西有问题」，重试多少次都一样，只会白等。"""
        self._add_usage("2026-10-14")
        self.fake.side_effect = uploader.UploadError(
            "服务端返回 422：user_key 必须是合法的 UUID", status=422)
        uploader.upload_pending(today=date(2026, 10, 14))
        # 一次就放弃：不重试
        self.assertEqual(self.fake.call_count, 1)

    def test_server_error_is_retried_up_to_the_limit(self):
        """500 要一直重试到上限为止。"""
        self._add_usage("2026-10-14")
        self.fake.side_effect = uploader.UploadError(
            "服务端返回 503：数据库写入失败", status=503)
        result = uploader.upload_pending(today=date(2026, 10, 14))
        self.assertFalse(result["ok"])
        self.assertEqual(self.fake.call_count, config.RETRY_COUNT + 1)

    def test_is_retryable_classification(self):
        """_is_retryable 的分类表：网络层失败/5xx/429 可重试，其余 4xx 不可。

        注意这些用例传的是 status 而不是「从文案里抠出来的数字」：
        文案是给人看的，随时可能改；重试策略必须挂在状态码上才稳。
        """
        def err(status):
            return uploader.UploadError("x", status=status)

        self.assertTrue(uploader._is_retryable(err(None)))    # 连不上/超时
        self.assertTrue(uploader._is_retryable(err(500)))
        self.assertTrue(uploader._is_retryable(err(503)))
        self.assertTrue(uploader._is_retryable(err(429)))     # 限流，等会儿再试
        self.assertFalse(uploader._is_retryable(err(400)))
        self.assertFalse(uploader._is_retryable(err(422)))

    # ── 提示文案 ──

    def test_describe_result_messages(self):
        """界面直接把这句话显示给用户，几种情况都要说得明白。"""
        self.assertIn("没有使用记录", uploader.describe_result(
            {"ok": True, "reason": "no_data", "days": 0, "apps": 0, "error": None}))
        self.assertIn("3 天", uploader.describe_result(
            {"ok": True, "reason": "ok", "days": 3, "apps": 9, "error": None}))
        self.assertIn("已关闭", uploader.describe_result(
            {"ok": False, "reason": "disabled", "days": 0, "apps": 0, "error": None}))
        self.assertIn("网络炸了", uploader.describe_result(
            {"ok": False, "reason": "error", "days": 0, "apps": 0, "error": "网络炸了"}))

    # ── 后台调度器 ──

    def test_scheduler_swallows_errors(self):
        """后台线程里的异常必须兜住，否则线程直接死掉、之后再也不上传。"""
        original = uploader.upload_pending
        self.addCleanup(setattr, uploader, "upload_pending", original)
        uploader.upload_pending = mock.Mock(side_effect=RuntimeError("模拟数据库炸了"))

        sched = uploader.UploadScheduler()
        result = sched.run_once()
        self.assertFalse(result["ok"])
        self.assertIn("模拟数据库炸了", result["error"])

    def test_scheduler_returns_result(self):
        """跑一轮后 last_result 里应该能看到结果，供界面显示。"""
        self._add_usage("2026-10-14")
        sched = uploader.UploadScheduler()
        result = sched.run_once()
        self.assertTrue(result["ok"])
        self.assertIs(sched.last_result, result)

    def test_scheduler_respects_disabled(self):
        """关掉开关后调度器什么都不做。"""
        self._add_usage("2026-10-14")
        storage.set_setting("upload_enabled", "0")
        sched = uploader.UploadScheduler()
        self.assertEqual(sched.run_once()["reason"], "disabled")
        self.assertEqual(self.requests, [])

    def test_concurrent_upload_is_refused(self):
        """已经在传的时候再点一次，应该直接返回 busy 而不是并发改水位线。"""
        self._add_usage("2026-10-14")
        holder = {}

        def slow(url, payload, timeout):
            # 在「第一次上传进行中」的时候再发起一次，模拟用户连点
            holder["second"] = uploader.upload_pending(
                today=date(2026, 10, 14), force=True)
            return self._fake_ok(url, payload, timeout)

        uploader._post_json = slow
        uploader.upload_pending(today=date(2026, 10, 14), force=True)
        self.assertEqual(holder["second"]["reason"], "busy")

    # ── 订阅（邮箱 + 开关） ──

    def test_subscribed_defaults_to_off(self):
        """从没设置过就是「没订阅」。默认必须是关的 —— 悄悄替用户订阅
        等于把他的邮箱拿去发邮件。"""
        self.assertFalse(uploader.is_subscribed())
        self.assertIsNone(uploader.get_email())
        self.assertEqual(uploader.subscription_state(),
                         {"email": None, "subscribed": False})

    def test_set_subscribed_round_trips(self):
        uploader.set_subscribed(True)
        self.assertTrue(uploader.is_subscribed())
        uploader.set_subscribed(False)
        self.assertFalse(uploader.is_subscribed())

    def test_set_subscribed_tolerates_truthy_junk(self):
        """界面传下来的是 Qt 的 bool，但别的地方可能给个字符串，
        存进去的必须归一成 "1"/"0"，否则 is_subscribed 的 == "1" 会漏掉。"""
        uploader.set_subscribed("yes")
        self.assertEqual(storage.get_setting("subscribe_enabled"), "1")
        self.assertTrue(uploader.is_subscribed())

    def test_looks_like_email_shape(self):
        self.assertTrue(uploader.looks_like_email("a@b.com"))
        self.assertTrue(uploader.looks_like_email("  a@b.com  "))   # 前后空格要剥掉
        self.assertTrue(uploader.looks_like_email("很奇怪但合法@example.com"))
        self.assertFalse(uploader.looks_like_email(None))
        self.assertFalse(uploader.looks_like_email(""))
        self.assertFalse(uploader.looks_like_email("   "))
        self.assertFalse(uploader.looks_like_email("没有at符号"))
        self.assertFalse(uploader.looks_like_email("@example.com"))
        self.assertFalse(uploader.looks_like_email("user@"))

    def test_email_rule_matches_server_exactly(self):
        """客户端的邮箱规则必须和服务端**逐字一致**。

        这条测试的存在理由：两边一旦漂移，客户端更严会把合法地址挡在外面
        （用户没有任何办法绕过），更松则会把注定 422 的东西发出去
        （用户看到一句看不懂的报错）。所以直接把两边跑一遍对比。
        """
        # server 是扁平导入，tests/__init__.py 已经把 server/ 放进了 sys.path
        from api.schemas import UploadRequest

        samples = ["a@b.com", "  a@b.com  ", "没有at符号", "@example.com",
                   "user@", "", "   "]
        for value in samples:
            with self.subTest(value=value):
                client_ok = uploader.looks_like_email(value)
                try:
                    UploadRequest._email_shape(value)
                    server_ok = bool((value or "").strip())
                except ValueError:
                    server_ok = False
                self.assertEqual(client_ok, server_ok,
                                 f"两端对 {value!r} 的看法不一致")

    # ── 上传载荷里的邮箱 ──

    def test_payload_carries_email_only_when_subscribed(self):
        """没订阅时载荷里必须是 None，不能把邮箱顺手带上去。

        email 字段留在上传体里只是为了「服务端数据库被清空时自愈」，
        它不是订阅通道（那是 update_subscription）。既然用户没订阅，
        就没有任何理由让他的邮箱跟着使用记录一起出本机。
        """
        self._add_usage("2026-10-14")
        uploader.set_email("me@example.com")     # 填了邮箱但**没**订阅

        uploader.upload_pending(today=date(2026, 10, 14), force=True)
        self.assertIsNone(self.requests[-1]["email"])

        uploader.set_subscribed(True)
        uploader.upload_pending(today=date(2026, 10, 14), force=True)
        self.assertEqual(self.requests[-1]["email"], "me@example.com")

    # ── update_subscription ──

    def test_update_subscription_saves_locally_and_syncs(self):
        result = uploader.update_subscription("me@example.com", True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["synced"])
        self.assertEqual(result["reason"], "ok")
        self.assertTrue(uploader.is_subscribed())
        self.assertEqual(uploader.get_email(), "me@example.com")
        self.assertEqual(self.requests, [{"email": "me@example.com",
                                          "subscribed": True}])
        self.assertFalse(uploader.subscription_pending())

    def test_update_subscription_posts_to_the_device_url(self):
        """URL 要带上本机自己的 user_key —— 打错了就是替别人改订阅。"""
        key = uploader.get_or_create_user_key()
        seen = {}
        original = uploader._post_json

        def spy(url, payload, timeout):
            seen["url"] = url
            return self._fake_ok(url, payload, timeout)

        uploader._post_json = spy
        self.addCleanup(setattr, uploader, "_post_json", original)
        uploader.update_subscription("me@example.com", True)
        self.assertEqual(seen["url"], config.subscription_url(key))
        self.assertIn(key, seen["url"])

    def test_update_subscription_refuses_without_email(self):
        """勾了订阅却没填邮箱：本地就该拦住，不要白发一个请求。"""
        result = uploader.update_subscription("", True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_email")
        self.assertEqual(self.requests, [])
        self.assertFalse(uploader.is_subscribed())

    def test_update_subscription_refuses_bad_email(self):
        result = uploader.update_subscription("不是邮箱", True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "bad_email")
        self.assertEqual(self.requests, [])

    def test_unsubscribing_keeps_the_email_on_file(self):
        """退订（subscribed=False）时邮箱要留着。

        留着是为了「手滑退了再订回来」不用重打一遍；服务端也只按
        subscribed 决定发不发信，留着邮箱本身不会产生任何邮件。
        """
        uploader.update_subscription("me@example.com", True)
        self.requests.clear()

        result = uploader.update_subscription("me@example.com", False)
        self.assertTrue(result["ok"])
        self.assertFalse(result["subscribed"])
        self.assertFalse(uploader.is_subscribed())
        self.assertEqual(uploader.get_email(), "me@example.com")

    # ── 服务端不可用 / 还没上传过：本地照存不误 ──
    #
    # 这一组挡的是设计要求里最要紧的一条：**除了上传数据，其它功能都要能
    # 独立运行**。填订阅邮箱不该因为「服务端连不上」或者「还没上传过」就失败 ——
    # 「先填邮箱、后上传」是很自然的顺序，不能是个死胡同。

    def test_subscribe_before_first_upload_is_saved_locally(self):
        """还没上传过就订阅：服务端 404，但本机必须记下来。"""
        self.fake.side_effect = uploader.UploadError("服务端返回 404", status=404)
        result = uploader.update_subscription("me@example.com", True)

        self.assertTrue(result["ok"], "本机应该存下了")
        self.assertFalse(result["synced"], "服务端显然还不知道")
        self.assertEqual(result["reason"], "pending_upload")
        self.assertTrue(uploader.is_subscribed())
        self.assertEqual(uploader.get_email(), "me@example.com")
        self.assertTrue(uploader.subscription_pending())

    def test_offline_save_still_works_locally(self):
        """断网时点保存：本机记下，标记待同步，界面据此提示「还没同步」。"""
        self.fake.side_effect = uploader.UploadError("连不上服务端：测试")
        result = uploader.update_subscription("me@example.com", True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "offline")
        self.assertFalse(result["synced"])
        self.assertTrue(uploader.is_subscribed())
        self.assertTrue(uploader.subscription_pending())

    def test_update_subscription_does_not_retry_404(self):
        """404 是 4xx，重试没有意义，只发一次请求。"""
        self.fake.side_effect = uploader.UploadError("服务端返回 404", status=404)
        uploader.update_subscription("me@example.com", True)
        self.assertEqual(self.fake.call_count, 1)

    def test_update_subscription_retries_server_errors(self):
        """5xx 要重试到上限 —— 服务端刚重启的几秒钟不该让用户白填一次。"""
        self.fake.side_effect = uploader.UploadError("服务端返回 503", status=503)
        result = uploader.update_subscription("me@example.com", True)
        self.assertTrue(result["ok"])          # 本机存下了
        self.assertFalse(result["synced"])     # 但服务端没有
        self.assertEqual(self.fake.call_count, config.RETRY_COUNT + 1)

    def test_update_subscription_never_raises(self):
        """它跑在后台线程里，抛出去只会把线程干掉、界面永远停在「保存中…」。"""
        def boom(url, payload, timeout):
            raise RuntimeError("模拟内部炸了")

        uploader._post_json = boom
        result = uploader.update_subscription("me@example.com", True)
        self.assertTrue(result["ok"])          # 本机照样存下
        self.assertFalse(result["synced"])
        self.assertIn("模拟内部炸了", result["error"])

    # ── 待同步的订阅设置靠下一次上传补上 ──

    def test_upload_flushes_pending_subscription(self):
        """离线时填的邮箱，下一次上传成功时应该自动补同步过去。

        这是「本地优先」能成立的前提：本机随时能改，服务端迟早会知道。
        """
        self._add_usage("2026-10-14")
        self.fake.side_effect = uploader.UploadError("连不上服务端：测试")
        uploader.update_subscription("me@example.com", True)
        self.assertTrue(uploader.subscription_pending())

        # 服务端恢复了
        self.fake.side_effect = None
        self.fake.side_effect = self._fake_ok
        self.requests.clear()
        result = uploader.upload_pending(today=date(2026, 10, 14), force=True)

        self.assertTrue(result["ok"])
        # 上传体 + 一次补同步 = 两个请求
        self.assertEqual(len(self.requests), 2)
        self.assertIn({"email": "me@example.com", "subscribed": True},
                      self.requests)
        self.assertFalse(uploader.subscription_pending())

    def test_upload_skips_flush_when_nothing_pending(self):
        """没有待同步的东西时不该多打一个请求。"""
        self._add_usage("2026-10-14")
        uploader.update_subscription("me@example.com", True)   # 直接同步成功
        self.requests.clear()

        uploader.upload_pending(today=date(2026, 10, 14), force=True)
        self.assertEqual(len(self.requests), 1)                # 只有上传那一次

    def test_failed_flush_does_not_fail_the_upload(self):
        """补同步失败不能把一次成功的上传报成失败 —— 它是搭便车的。"""
        self._add_usage("2026-10-14")
        self.fake.side_effect = uploader.UploadError("连不上服务端：测试")
        uploader.update_subscription("me@example.com", True)

        def upload_ok_but_subscribe_boom(url, payload, timeout):
            if "user_key" in payload:          # 上传请求
                return self._fake_ok(url, payload, timeout)
            raise uploader.UploadError("订阅接口炸了")   # 补同步那一次

        self.requests.clear()
        uploader._post_json = upload_ok_but_subscribe_boom
        result = uploader.upload_pending(today=date(2026, 10, 14), force=True)

        self.assertTrue(result["ok"], "上传成功了就是成功了")
        self.assertEqual(result["days"], 1)
        self.assertTrue(uploader.subscription_pending(), "还得留着下次再补")

    def test_flush_syncs_unsubscribe_too(self):
        """退订也要能补同步 —— 上传接口永远不会把 subscribed 改回 0。"""
        uploader.update_subscription("me@example.com", True)
        self.fake.side_effect = uploader.UploadError("连不上服务端：测试")
        uploader.update_subscription("me@example.com", False)
        self.assertTrue(uploader.subscription_pending())

        self.fake.side_effect = self._fake_ok
        self.requests.clear()
        self.assertTrue(uploader.flush_subscription())
        self.assertEqual(self.requests,
                         [{"email": "me@example.com", "subscribed": False}])
        self.assertFalse(uploader.subscription_pending())

    def test_describe_subscription_messages(self):
        """这几句话是直接显示给用户看的，三种情况都要说得明白。

        最要紧的是别把「本机存下了」说成「已生效」—— 用户会等一晚上邮件，
        然后以为程序坏了。
        """
        self.assertIn("已订阅", uploader.describe_subscription(
            {"ok": True, "synced": True, "reason": "ok", "error": None,
             "email": "me@example.com", "subscribed": True}))
        self.assertIn("退订", uploader.describe_subscription(
            {"ok": True, "synced": True, "reason": "ok", "error": None,
             "email": "me@example.com", "subscribed": False}))

        # 本机存下了、服务端还不知道：必须点明「还没同步」，而不能说「已订阅」
        pending = uploader.describe_subscription(
            {"ok": True, "synced": False, "reason": "pending_upload",
             "error": None, "email": "me@example.com", "subscribed": True})
        self.assertIn("本机", pending)
        self.assertIn("同步", pending)
        self.assertNotIn("已订阅", pending)

        offline = uploader.describe_subscription(
            {"ok": True, "synced": False, "reason": "offline", "error": None,
             "email": "me@example.com", "subscribed": False})
        self.assertIn("本机", offline)
        self.assertIn("同步", offline)

        # 本机都没存下的（格式不对），必须把错在哪说清楚
        self.assertIn("邮箱", uploader.describe_subscription(
            {"ok": False, "synced": False, "reason": "bad_email",
             "error": "「x」看起来不是一个邮箱地址", "email": "x",
             "subscribed": True}))
        # ok=False 但 error 是空的（不正常，但不该显示 "None"）
        self.assertIn("保存失败", uploader.describe_subscription(
            {"ok": False, "synced": False, "reason": "error", "error": None,
             "email": None, "subscribed": False}))


if __name__ == "__main__":
    unittest.main()
