# 联展经营与签账资料

`data/` 为离线合成业务记录。文件均为 UTF-8 的 Tab 分隔表，首行是列名，逗号分隔集合，`-` 表示不适用，`?` 表示未取得，不把它们当作零。标识符只在各表声明的归属范围内有效。

## 运行与资料核对

使用 Python 3.11。分析环境另附固定版本的数值与统计依赖，可在本地虚拟环境执行 `python3 -m pip install -r requirements.txt`。

```sh
python3 inspect_packet.py
python3 -m unittest discover -s tests
```

前一条命令只检查表头和记录形状并显示行数。可传入另一目录读取同格式记录。当前没有分析、决策或业务处理结果。

## 输入表

- `sessions.tsv`：同一馆厅、分档为候选比较条件；format=show/ordinary；traffic 为计数，observed 是是否完整。 列顺序为 `session`、`period`、`hall`、`band`、`format`、`traffic`、`observed`。
- `claims.tsv`：tender 为真实收款编号；同 tender 不是两笔款。ticket 是联票号或空；kind=payment/reversal/reported。金额为整数分。 列顺序为 `proof`、`tender`、`ticket`、`session`、`studio`、`kind`、`amount`、`observed`。
- `tickets.tsv`：admitted 取 yes/no/transfer；evidence 标记确认级别。收到时序 separate 见凭证。 列顺序为 `ticket`、`session`、`holder`、`admitted`、`day`、`evidence`。
- `shared.tsv`：会计约定分担比例 studio:weight，以相应受益量分担；unit=direct 为边际开支，shared 为共享费用，不等同边际开支。 列顺序为 `period`、`service`、`amount`、`studios`、`allocation`、`unit`。
- `paid.tsv`：got 为已发金额（分）；reserve 是已留置非已付。只对原凭证的闭账操作有效。 列顺序为 `closure`、`period`、`tender`、`studio`、`got`、`paid_at`、`state`。
- `amend.tsv`：确认可用于改归的凭证按收到时点改变事实；不更改原历史出账。 列顺序为 `amend`、`tender`、`session`、`studio`、`received`、`evidence`。
