# servedoctor

**这份压测报告测的是服务，还是压测器？**

指向一次推理服务压测的输出。它先判断施加的负载是否独立于服务器自身的速度，再判断
这份样本能不能撑起报告里印出来的那个分位数，最后判断这些数字能不能和别的数字比。
答案是否的时候，它说出需要什么才能。

零依赖，Python 3.9+，CI 纯 CPU。

```console
$ servedoctor audit tests/fixtures/EXAMPLE_closed_loop.csv --rate 6.4 --concurrency 7
closure: [violation] occupancy sits at 7 for 99.5% of the run, and every send lands a
  median of 0.000 inter-send gaps after a completion -- that is a worker pool of 7, not
  a schedule. [...]
q99: [warn] q99=2.8589s, 90% CI [2.6513, 10.7950] from order statistics 1972-1988 of
  2000 (21 observations in the tail, interval is 285% of the point estimate).
```

最后一行就是整个库的缩影：报告印出 P99 = 2.86 秒，而它自己的 90% 置信区间一直伸到
10.80 秒。**这个数不是"略有不确定"，它和一个四倍大的值同样相容**，而它来源的那份报告
里没有任何一句话提到这件事。

---

## 三个发现

### ① 同一台服务器，闭环压测报出的尾延迟短 16.5 倍

`examples/coordinated_omission.py` 在任何有 Python 的机器上都能跑，不需要 GPU、模型或
网络：一个 8 槽位、单请求 80 ms、每 10 秒冻结 1.2 秒的 mock 服务器。两个压测器都按
80 req/s 配置——一个开环，一个用运维会算出来的 6 个 worker（`80 × 0.080s`）。
本机连续四轮：

| | 实际投递速率 | q50 | **q99** | 最大值 |
|---|---|---|---|---|
| 闭环，6 worker | 65.2 req/s（**只有配置值的 81%**） | 0.082 s | **0.084 s** | 1.28 s |
| 开环，80 req/s | 77.5 req/s（97%） | 0.66 s | **1.39 s** | 2.37 s |

**q99 差 16.5 倍，最大值只差 1.8 倍。**

闭环**看见了**那次停顿——它就在最差值里。它只是从来没有足够多的请求暴露在一次停顿
下，让停顿够得着一个百分位：6 个 worker × 2 次停顿 = 1500 个请求的 0.80%，而 P99 住在
最上面的 1% 里。开环每次停顿暴露 96 个，12.8%。

这比"尾巴被截断了"精确，而且能直接拿去用在别人的报告上：**闭环压测里，最大值和 P99
之间的巨大落差本身就是警报。**

同一次运行还掉出两条。闭环悄悄只投递了配置速率的 81%——因为闭环的速率是服务器速度的
**输出**而不是输入，所以它从来没测过它被要求测的那个负载。以及：闭环连自己的 P99 都
框不住，90% 区间跨了 15 倍，因为那个分布是两根尖峰、中间什么都没有。

四轮比值：16.52× / 16.63× / 16.53× / 16.48×，最大值比 1.86× / 1.69× / 1.76× / 1.88×。

### ② P99 要 299 个请求才开始有上界

这不是经验法则。不超过真实 q 分位数的观测个数服从 Binomial(n, q)，所以分位数的置信
区间就是从这个二项分布两侧尾巴上取的一对次序统计量。上界的秩落在样本内，当且仅当

```
q**n <= alpha/2        即   n >= ln(alpha/2) / ln(q)
```

q=0.99、90% 置信 → **n ≥ 299**。95% 要 368。P99.9 要 2995。

低于这个数，最大的那个观测**不是** P99 的上界，它只是抽到的最大的那个。所以工具拒绝
报这个数，并说出这份样本能撑起哪一个：

```console
q99: [violation] n=200 but a bounded 90% interval on q99 needs n>=299. [...]
  This sample can carry q98.51 -- report that, or send 99 more requests.
```

没有 bootstrap、没有重采样、没有随机种子：这个区间是 n 和 q 的确定函数，所以测试能把
它一位不差地钉死。

### ③ 2% 的超时不是让 P99 有偏，是把 P99 删掉

失败的、以及压测结束时还没跑完的请求，会在算统计量之前被丢掉。它们不是随机子集：
**一个"没跑完"的请求，按定义比每一个跑完的都慢**。丢掉最上面的 `f` 比例，报出来的
q 分位数其实是 `q(1-f)` 分位数——而当 `q > 1-f` 时，你要的那个分位数**整个落在被丢掉
的那部分里**。

```console
censoring: [violation] 12 of 600 requests (2.00%) did not finish and were dropped.
  [...] the top 2.00% of the true distribution is exactly the part that was discarded
  -- and q99 lies inside it. The observed q99 of 11.3064s is not an estimate of the
  true one; it is the q99 of the survivors.
```

同一份文件上问 q95，修正是存在的，工具就给出修正——因为 0.95 < 0.98。这个拒绝不是
政策，是算术走到头了。

---

## 十四条规则

| | |
|---|---|
| **SD001** | 这份文件是哪一类证据，因而能得出什么结论 |
| **SD002** | 开环还是闭环，两个互相独立的判据 |
| **SD003** | coordinated omission：从发出时刻算的延迟 vs 从该发的时刻算的 |
| **SD004** | 样本撑不撑得起这个分位数——精确区间，n<299 直接拒报 |
| **SD005** | 冷启动被留在了稳态分布里 |
| **SD006** | 这次运行有多少是爬坡和排空，而不是稳态 |
| **SD007** | 失败与未完成的请求被从它们定义的那条尾巴里丢掉了 |
| **SD008** | TPOT 的分母用了 m 而不是 m−1 |
| **SD009** | prompt token 和 output token 被加进同一个速率 |
| **SD010** | Little's Law 对声明并发——以及它在哪里只是个恒等式 |
| **SD011** | 到达过程，每一条排队论结论都依赖它 |
| **SD012** | 利用率与它蕴含的排队延迟 |
| **SD013** | SLO 达成率是分位数上的事，以及 goodput 而不是 throughput |
| **SD014** | 工具读不出来的字段——也就是没人核过单位的那些字段 |

`docs/rules.md` 给每一条出处。其中三条写着 **出处：本仓库**，因为它们是写这个库的时候
自己犯的。

---

## goodput：吞吐报告推荐的那一档

```console
$ servedoctor sweep tests/fixtures/EXAMPLE_ladder_*.csv --e2e-ms 3000
ladder_40:  2.38 req/s offered, throughput 2.38, goodput 2.17, attainment 90.8%
ladder_70:  4.41 req/s offered, throughput 4.41, goodput 3.86, attainment 87.4%
ladder_95:  5.76 req/s offered, throughput 5.76, goodput 3.02, attainment 52.4%
ladder_114: 6.17 req/s offered, throughput 6.17, goodput 2.28, attainment 37%
knee: [violation] throughput peaks at ladder_114 but goodput peaks at ladder_70.
```

这两档之间，吞吐涨了 40%，goodput 掉了 41%。

---

## 这个工具做不到什么

1. **闭环日志只能被标注，不能被修正。** 没有记录下来的时间表就没有可对照的基准，工具
   不会替你造一个。`--rate` 给出的界（"有多少请求根本没发出去"）是下界，不是损失。
2. **利用率需要 `--capacity`。** 在任何会 batch 的服务器上 `λ·E[S]` 是"施加的并发"而
   不是利用率——本文件的早期版本把它 clamp 之后，给一次尾延迟只有 13 秒的运行算出了
   398 秒的排队"下界"。
3. **排队公式是个形状，不是 batching 服务器的模型。** M/M/1 和 M/D/1 描述的是带准入
   队列的单服务器，continuous batching 不是那个东西。
4. **开环和闭环并不总是可区分的。** 撞上并发上限的限速压测器在那之后**就是**闭环；
   周期严格等于服务时间的固定速率schedule 在构造上和闭环无法分辨。两者都返回
   `inconclusive`——那和两个答案中的任何一个都不同。
5. **这里没有任何东西检查返回内容对不对。** 一个飞快返回垃圾的服务会拿到漂亮的分数。

---

## 七部曲里的第七部，也是最后一部

**nodebench**（测量一台节点）→ **benchdoctor**（读测量脚本，静态找坑）→
**tracedoctor**（读 kernel trace，动态找坑）→ **regressiondoctor**（这次比上次差，
是真差还是噪声）→ **fitdoctor**（这个数本来该是多少）→ **telemetrydoctor**（这个数
我能拿去做什么运算）→ **servedoctor**（这份报告测的是服务还是压测器）。

诚实性字段是同一个想法的第四代：

| 项目 | 字段 | 回答什么 |
|---|---|---|
| regressiondoctor | `noise_basis` | 这条判定的阈值是实测的还是假设的 |
| fitdoctor | `basis` | 这个数字是实测 / 标称 / 推导 / 假设 |
| telemetrydoctor | aggregation contract | 这个数字我能拿去做什么运算 |
| servedoctor | `evidence` | **这份文件到底能支撑什么结论** |

七个项目的关键数字汇总在一页上：nodebench 的 `docs/dashboard.html`。

## 姊妹项目

- [nodebench](https://github.com/liu-perf/nodebench) — 五分钟测完一整台多卡节点
- [benchdoctor](https://github.com/liu-perf/benchdoctor) — 静态审计测量脚本，13→0 假阳性
- [tracedoctor](https://github.com/liu-perf/tracedoctor) — 读 nsys trace，抓静态看不见的坑
- [regressiondoctor](https://github.com/liu-perf/regressiondoctor) — 真退化还是噪声
- [fitdoctor](https://github.com/liu-perf/fitdoctor) — 显存账本与 roofline
- [telemetrydoctor](https://github.com/liu-perf/telemetrydoctor) — 监控列的聚合契约
- [queuebound](https://github.com/liu-perf/queuebound) — **最近的邻居**：servedoctor 审「这次压测有没有测到它声称的东西」，queuebound 接「压测没问题的前提下能推出什么」。它拿 Little 定律给吞吐配一条实测误差棒（留出验证最差 3.84%），并且对 p99 **拿不出值**——因为 `p99 = mean × ln(100)` 在那批数据上从 −59% 偏到 +154%，两个方向都错。

## 深入

- [closed-vs-open.md](closed-vs-open.md) — 16.5 倍那个实验的完整过程，含被否掉的第一版判据
- [quantiles.md](quantiles.md) — 299 是怎么解出来的，以及删掉的 P99
- [rules.md](rules.md) — 十四条规则逐条出处
- [queueing.md](queueing.md) — 利用率与尾延迟是同一个旋钮
