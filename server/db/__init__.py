"""服务端数据层：MySQL 连接、建表，以及各张表的数据访问。

和客户端的 client/core/storage.py 是两回事，别混：
    * client/core/storage.py 是每台机器本地的 SQLite，只存自己这台的数据；
    * 这里存的是所有用户的数据，靠 user_key 区分。

按「一张表一个文件」拆开：
    conn.py           连接、建库建表、行转 JSON —— 别的子模块都靠它
    schema.sql        表结构定义（唯一的一份，紧挨 conn.py 放）
    users.py          users 表：登记用户、查用户、订阅名单
    usage_records.py  usage_records 表：写入某天的明细、按天/按区间读
    mail_log.py       mail_log 表：报告发过没有、发信结果记录

**这个包里故意没有 re-export 门面**：`db/__init__.py` 里**不**写
`from db.users import get_user` 这类再导出。原因是那会造出**第二个绑定**——
`db.get_user` 和 `db.users.get_user` 会变成两个各自独立的名字，而
`db.users.set_subscription` 内部查的是自己模块的全局。于是
`mock.patch.object(db, "get_user")` 会**成功但无效**：测试以为挡住了数据库，
实际函数还是走真实连接去打线上那台 MySQL。

不做门面的话每个函数只有**一个**绑定位置，patch 打错地方会直接
AttributeError 响亮地报错，而不是静默地测了个假的。

子模块之间引用也守同一条规矩：写 `from db import conn` 然后 `conn.connect()`，
**不要**写 `from db.conn import connect`——后者会把 connect 复制一份进本模块的
全局，patch `db.conn.connect` 就再也影响不到这里了。
"""
