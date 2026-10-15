"""HTTP 接口层：路由、请求/响应结构、鉴权依赖。

这个包只负责「把 HTTP 请求翻译成对 db / mail 的调用」，不写 SQL、不渲染邮件正文。

分文件的原则是「一个文件管一类资源」：
    deps.py       管理接口的口令校验（被多个路由模块共用）
    schemas.py    所有请求/响应体（Pydantic 模型），客户端照着它构造请求
    health.py     GET /                 服务目录（人肉访问用）
                  GET /api/v1/health    探活（含数据库连通性）
    upload.py     POST /api/v1/upload   客户端上传当天数据
    usage.py      GET  /api/v1/usage/{user_key}    明细
                  GET  /api/v1/summary/{user_key}  某天汇总
    users.py      GET  /api/v1/users                用户列表（管理）
                  GET  /api/v1/users/{user_key}     单个用户（管理）
                  POST /api/v1/subscription/{user_key}  设置邮箱/订阅
    admin_mail.py POST /api/v1/admin/daily-mail     手工触发报告邮件（管理）
                  GET  /api/v1/admin/mail-log       发送记录（管理）

注意各模块里的 router 是在装饰器上写**完整路径**（含 /api/v1 前缀）的，
app.py 挂载时**不加 prefix**——因为 health.py 同时管着 `/` 和 `/api/v1/health`，
两者没有共同前缀。加 tags 也会改变 OpenAPI 文档内容，所以也不加。
"""
