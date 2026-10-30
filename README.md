# oi事实监测播报

本地工具。读币安 U 本位永续的公开行情，监测未平仓合约，不是账户里持有的币。10% 和 5% 看未平仓金额涨跌，再配合同一段的价格变化，越过阈值就用中文播出来。

不需要币安 API Key。扫描不会下单。手动下单是单独的一步，默认关闭。

## 监测的是什么

- 未平仓金额：`sumOpenInterestValue`，未平仓合约换成 USDT 的金额。10%、5% 的门槛、四种形态里的涨跌、分档、翻倍、2000 万分水岭、5 分钟刻度，都看这个金额。
- 未平仓合约数量：`sumOpenInterest`，还没平掉的合约张数。页面上单独一列，只作对照，不参与 10% 和 5%。
- 流通量只用来算市值，不是未平仓合约，也不是持有数量。

门槛按第 5 次刻度的未平仓金额分档。比较的是第 1 次到第 5 次的未平仓金额涨跌，价格也用这两次：

- 不到 500 万：15%
- 500 万到 1000 万：10%
- 1000 万以上到 1500 万：5%
- 1500 万以上：5%

四种形态：

- 未平仓金额涨、价格涨：优先推报，做多信号
- 未平仓金额涨、价格跌或平：可能为出货、洗盘或蓄力
- 未平仓金额跌、价格跌：弃盘行情，需观望
- 未平仓金额跌、价格涨：未平仓减少、价格上涨，需观望

另外两条特殊提醒：第 1 次到第 5 次未平仓金额翻倍，并且第 5 次至少 500 万，标成最高级别。未平仓金额从 2000 万以下跨到 2000 万或以上，单独报关键分水岭。

价格绝对变动小于 0.5% 算平。等于 0.5% 算涨或跌。扫描默认看成交额前 400。

页面上的 5 分钟刻度只做减法：5 分钟前的未平仓金额是 A，现在是 B，刻度是 B 减 A。历史用 5 分钟一根。

## 用到的公开接口

都是币安现在公开、免费的行情接口，按 IP 限制访问次数，不按账户收费。

U 本位合约：

- `GET /fapi/v1/exchangeInfo`：找出正在交易的 USDT 永续
- `GET /fapi/v1/ticker/24hr`：最新价、24 小时涨跌、成交额。不带 symbol 时一次返回全部，权重 40
- `GET /fapi/v1/premiumIndex`：标记价格和 `lastFundingRate`。不带 symbol 时一次返回全部，权重 10。不按币种去拉资金费率历史，那条线另外限 500 次 / 5 分钟
- `GET /futures/data/openInterestHist`：`sumOpenInterest`、`sumOpenInterestValue`、`CMCCirculatingSupply`。周期用 `5m`。这条线的 IP 限额是 1000 次 / 5 分钟，程序本地先卡在 800 次 / 5 分钟

合约 IP 权重大约 2400 / 分钟，响应头 `X-MBX-USED-WEIGHT-1M` 能看到已经用了多少。未平仓合约历史不走这套权重，单独计数。

现货 REST 没有未平仓合约。这套判断比较的是未平仓合约和合约价格，所以不去打现货接口，避免多占请求次数。

## 运行

需要 Python 3.10 或更新版本，没有第三方依赖。

```bash
cd binance-oi-monitor
python3 server.py
```

浏览器打开 http://127.0.0.1:8765

长时间开着可以用 `run.sh`。它继承当前环境变量（包括 `OI_WEBHOOK_URL` 和 `OI_WEBHOOK_TOKEN`），`server.py` 异常退出后 2 秒再拉起。Ctrl+C 正常停掉则不再重启。

```bash
./run.sh
```

`/api/health` 里有最近一次成功扫描：`lastScanAt`（毫秒）、`scanAgeSec`、`stale`。扫描每 5 分钟一轮。超过 `OI_SCAN_STALE_SEC`（默认 600 秒）还没有新的成功扫描，`stale` 为 true。扫描线程如果已经没了，看门狗会再开一条；如果线程还卡在网络调用里，进程以退出码 1 结束，交给 `run.sh` 或外部监督进程重启。

币安请求的连接和读取都有超时，整个请求（含域名解析）还有一个硬截止，避免 `urlopen` 一直卡着。超时或网络错误会换下一个币安地址。限流最多再试 4 次，不会无限重试。一轮扫描超过 240 秒就先返回已经拿到的合约，剩下的等下一轮。

只在终端看一眼：

```bash
python3 server.py --once --limit 30
```

`--limit` 按 24 小时成交额取前 N 个合约，也可以写成 `all`。默认 400。全市场大约 500 次未平仓合约历史请求，会顶到 5 分钟限额，所以播报按每 5 分钟一轮来拉。

## 播报

未平仓历史是 5 分钟一根。每 5 分钟拉一次，记下这一轮的刻度。30 分钟为一轮，前 25 分钟记下 5 次，最后 5 分钟只用第 1 次和第 5 次核实门槛。未平仓金额和价格都是 (第 5 次 − 第 1 次) / 第 1 次。中间三次只显示，不参与门槛。过线才播这一轮。

页面仍然每 60 秒刷新。勾选「语音播报」后，新消息会用浏览器中文语音读出来。打开页面时不会把历史消息再读一遍。

每天的监测按北京时间写成 Markdown，留在这台云电脑的 `/cursor/stores/self/oi-memory/`。一天一个文件，里面有播报原文、所有越过门槛的合约、每一轮扫描。页面上的播报列表给浏览用，Markdown 不删。币安还留着的 5 分钟历史可以补进同一份文件，标记为回放。目录可以用环境变量 `OI_MEMORY_DIR` 改。

一条消息的样子：

```text
🟢 [BTCUSDT] 币安未平仓金额 +6.5%，过去 3600 秒价格上涨 5.2%，未平仓金额：76.00 亿美元，未平仓金额/市值比率：0.5%，24 小时价格变动：-4.2%。优先推报，做多信号
```

绿点是优先做多，或这 5 次刻度里未平仓金额翻倍。其余形态是白点。

## Webhook

每条播报可以再 POST 到你自己的接收地址。没配地址就不会发。请求体始终是这三项，没有令牌也不会多带字段：

```json
{"text": "播报原文", "symbol": "BTCUSDT", "id": 1}
```

接收端要求 `Authorization: Bearer <key>` 时，再配一个令牌。令牌放进 `Authorization`。它如果已经以 `Bearer ` 开头，不会再加一次前缀。

环境变量在进程启动时读入，并覆盖 `data/settings.json` 里已经保存的同名字段。地址只接受 http 或 https。启动之后如果再保存别的设置，当前生效的地址和令牌会一起写进这个文件；文件只在本机，接口响应里仍然是打码的。

```bash
export OI_WEBHOOK_URL=https://example.com/hooks/oi
export OI_WEBHOOK_TOKEN=your-secret
python3 server.py
```

运行中也可以改，对之后的播报立刻生效：

```bash
curl -s -X POST http://127.0.0.1:8765/api/settings \
  -H 'Content-Type: application/json' \
  -d '{"webhookUrl":"https://example.com/hooks/oi","webhookToken":"your-secret"}'
```

`/api/settings` 和 `/api/broadcasts` 返回的 `webhookUrl`、`webhookToken` 都是打码的：地址上的查询串换成 `***`，令牌换成 `***`（本来就带 `Bearer ` 时显示成 `Bearer ***`）。`webhookSet` 和 `webhookTokenSet` 表示有没有配置。把打码后的令牌原样提交不会盖掉已保存的值；传空字符串会清除。令牌不会出现在日志里。

发送放在后台，不挡住每 5 分钟的扫描。某条没送出去就记一行失败日志，日志里没有令牌；这一轮扫描照常结束，下一轮继续。

## 手动下单

下单接口只接受本机访问。默认 `TRADING_ENABLED` 不打开，播报线程不会发单。需要连你自己的账户时再说，公开监测用不到 API Key。

## 网络

程序会先试 `https://fapi.binance.com`。如果当前网络被币安按地区拒绝，会改走 `https://www.binance.com` 上的同一套公开接口。

```bash
export BINANCE_FAPI_BASE=https://fapi.binance.com
export https_proxy=http://127.0.0.1:7890
python3 server.py
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```
