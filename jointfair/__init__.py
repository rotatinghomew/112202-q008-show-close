"""联展经营结算引擎。

模块划分：
- records:  TSV 资料装载、口径解析（``-`` 不适用、``?`` 未取得，均不当零）与外键校验
- evidence: 截至日证据快照、共享团确认、凭证可行归属、对照窗口资格
- increment: 营业对照、反事实基准、新增净收益（面积/客流两种规模口径）
- settle:   保底、共同奖金池、单方上限、预留、已付、结转与调账单
- report:   人读 markdown 与机读 json 出账
"""

__all__ = ["records", "evidence", "increment", "settle", "report"]
