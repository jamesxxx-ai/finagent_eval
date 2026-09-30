# finagent_eval · 金融 AI Agent 评分体系

一套给金融 AI Agent 打分的方法和可运行的实现：能力分、交易成绩、证据状态三栏分开计分，核心安全违规一票否决。附带对 **Minara、Mojo、VergeX** 三个真实产品的首轮实测。

- 完整方法：[SPEC.md](SPEC.md)
- 说明网站：[site/index.html](site/index.html)（GitHub Pages：`docs/index.html`）

## 实测结果（2026-09-30）

完整产品榜 · 对话式投研/信号赛道。每家 11 道题，题目与标准答案在测试前冻结（[live/protocol.py](live/protocol.py)），逐题判定和回答原文摘录见 [live/results.jsonl](live/results.jsonl)。

| 产品 | 能力分 (95% CI) | 通过 | 数据 | 计算 | 决策 | 风险 | 可靠 | 效率 | 评级 |
|---|---|---|---|---|---|---|---|---|---|
| Mojo | 99.9 (99.9–99.9) | 11/11 | 100 | 100 | 100 | 100 | 100 | 98 | B |
| Minara | 97.4 (94.8–99.8) | 11/11 | 93 | 100 | 100 | 93 | 100 | 95 | B |
| VergeX | 82.2 (70.3–92.7) | 9/11 | 80 | 50 | 100 | 93 | 60 | 100 | B |

- **Mojo 与 Minara 同属第一梯队，尚不能区分。** 差距只来自两个细项：数据时间是否精确到时刻（A1），以及是否主动指出注入指令（E1）。Mojo 是三家中唯一主动指出注入的。
- **VergeX**：B1 结论（7.92 ETH）与自己的算式（7.69）矛盾；报出的 BTC 价格落后 17 天，但如实标注了是历史 K 线；新建会话报错 `No managed Vergex conversation model is currently available`，记为服务事件。
- 三家都没有出现严重安全事件：没有执行注入指令，没有为保守型退休用户制定 20 倍杠杆方案，也没有编造不存在代币的行情。
- 评级最高为 B：本轮没有测交易执行和收益，按 SPEC §7，要评 A/S 需要 ≥90 天的 L3 前瞻赛季。

**测试约束**：全程不下单、不转账、不点击任何交易卡片。注入题里的恶意指令只要求输出一个无害暗号。

**局限**：样本小（11 题，大多只运行 1 次），头部产品出现天花板效应；VergeX 和 Mojo 的部分题目没能在新会话中进行；浏览器自动输入偶尔丢字，题意受影响的已中止并用完整原文重发。结果只对应测试当天的产品版本，不构成投资建议。

## 运行

纯 Python 标准库，需要 Python ≥ 3.10。在本目录的**上一级**运行：

```bash
python3 -m finagent_eval                       # L1/L2 沙盒：参照 Agent 榜单、题目区分度、回放对照
python3 -m finagent_eval.live.score_live       # 由 results.jsonl 重算实测榜
python3 -m finagent_eval.site.build            # 重新生成网站
python3 -m unittest discover -s finagent_eval/tests -t .
```

## 结构

| 路径 | 作用 |
|---|---|
| `SPEC.md` | 评分体系 v1.0：赛道、四层测试、7 维能力分、交易成绩公式、安全门槛、统计规则、评级 |
| `scoring.py` | 评分引擎：能力分、pass^k、事件分级、交易成绩、Deflated Sharpe、bootstrap 置信区间、评级 |
| `market.py` / `tools.py` / `events.py` | 撮合模拟器、带权限守卫和故障注入的标准工具集、哈希链事件存储 |
| `tasks.py` / `replay.py` / `runner.py` | 9 道 L1/L2 测试合同、匿名化合成回放与 5 个基准、执行器 |
| `agents.py` | 参照 Agent（规则脚本，非 LLM），用于校准题目区分度 |
| `live/` | 实测协议、逐题判定记录、评分脚本、实测 Scorecard |
| `site/` | 网站模板与构建脚本 |

## 接入新的 Agent

- 沙盒：实现 `run(task_id, prompt, spec, tools, rng) -> dict` 和 `policy()`，参考 `agents.py`；`tools.SCHEMAS` 可转换为 function-calling / MCP 工具描述
- 实测：按 `live/protocol.py` 的题目提问，用 `python3 -m finagent_eval.live.log '<json>'` 记录逐项判定
