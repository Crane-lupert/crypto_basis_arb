# crypto_basis_arb (한국어)

[English README](README.md)

Binance BTC·ETH의 현물, USDⓈ-M 무기한 선물, 분기물을 대상으로, **기존 문헌의 이론 가격**(cost of carry,
He-Manela-Ross-von Wachter의 무기한 선물 공정가)과 **시장 베이시스**를 비교해 진입·청산하는 규칙을
결정론적 백테스트 → paper-live 데몬 → Binance testnet 주문 라우팅까지 **하나의 코드 경로**로 구현한
프로젝트입니다.

> 상태: 백테스트(spec v1.0.2 사전등록 셀), 비용·지연 진단, paper-live 데몬 구현 완료. paper 데몬은 smoke
> test만 했고, 연속 운영 기록과 testnet 실행 기록은 아직 없습니다(`docs/research_log.md`). 실자금 거래는 없으며,
> 이 코드는 구조적으로 실자금 엔드포인트에 주문을 보낼 수 없습니다(아래 *정직성과 한계* 참고).

## 이 프로젝트의 의의: 새 연구 결과가 아니라 "라이브에 가까운 구현"

**새로운 알파나 새로운 연구 결과를 찾는 프로젝트가 아닙니다.**

* 공정가 공식은 전부 기존 논문에서 가져왔습니다(출처와 URL: [`docs/theory.md`](docs/theory.md)). 전략
  규칙도 "왕복 비용을 보유 기간으로 나눈 hurdle을 넘으면 진입, 공정가로 돌아오면 청산"이라는 가장 단순한
  형태이고, 전체 표본을 보기 전에 고정했습니다(`git tag prereg-v1.0.2`).
* 백테스트 결과도 문헌을 **재확인**하는 수준입니다. funding carry는 2020-21년에 수익을 냈고 2024년 이후
  압축됐습니다(Schmeling-Schrimpf-Todorov, Borri-Liu-Tsyvinski-Wu). HMRW random-maturity 차익은
  수수료 수준이 수익 여부를 정합니다(HMRW). 둘 다 이미 알려진 결론입니다.

이 저장소에서 의미가 있는 부분은 **논문 공식을 실제로 돌아가는 운영 코드로 옮길 때** 생기는 문제를
하나씩 처리했다는 점입니다.

| 논문·단순 백테스트에서는 보통 생략되는 것 | 이 저장소에서 한 것 |
|---|---|
| 연구 코드와 운영 코드가 따로 놂 | live 데몬이 백테스트와 **같은 함수**(`basis_arb.model.signals`)를 import하고, **닫힌 1h 봉**에서만 결정. 기록된 봉을 한 개씩 재생해 백테스트와 같은 결정이 나오는지 테스트(`tests/test_live_daemon.py`) |
| 펀딩을 매 8h 평균값으로 근사 | `/fapi/v1/fundingRate`의 실제 정산 시각·실제 정산 금리로 적립. 4h·1h 펀딩 주기에도 맞도록 30일 달력 창 합계로 trailing 펀딩 계산 |
| 분기물 만기·롤·상장 공백 무시 | 보유 계약을 진입 시 만기로 키잉. 데이터 구멍은 HOLD로 처리(가짜 만기·가짜 롤 방지). 다음 분기물이 아직 상장 안 됐으면 롤 불가 |
| 거래소 장애 봉도 그대로 사용 | 현물이 멈춘 봉은 `stale_bar`로 표시하고 결정 금지(얼어붙은 현물 대비 최대 ~2%의 가짜 프리미엄 차단) |
| 청산 위험 무시 | isolated margin 청산 모니터(종가 기준, 마크 가격 근사). 분기물의 얇은 호가 wick은 진단용으로만 보고 |
| 고정 수량으로 노출이 불어남 | 노출이 목표 대비 25% 넘게 벗어나면 펀딩 시각에 두 다리를 목표 노출로 재조정(수수료 반영) |
| 원시 수익률·봉 단위 Sharpe | 헤드라인은 **T-bill 초과 수익**, 일별 집계 × √365 Sharpe. 포지션이 없는 날도 자본의 기회비용을 차감 |
| 불확실할 때 그냥 진행 | **fail-secure** kill switch: 호가 5분 초과 지연 → SAFE(신규 진입 금지), 30분 초과 → HALT(오래된 가격으로 헤지를 풀지 않고 정지), 결정 경로 예외 → SAFE, 낙폭 5% → SAFE |
| 키·실거래 경로가 코드에 섞임 | `live/broker.py`에는 mainnet 거래 호스트 문자열 자체가 없음(테스트로 강제). testnet 키는 환경변수에서만 읽음 |
| 실행할 때마다 결과가 조금씩 다름 | `run_backtest(panel, cfg)`는 순수 함수. 두 번 실행하면 equity 시계열과 거래 CSV가 바이트 단위로 동일(테스트로 강제) |
| 결과를 보고 파라미터 조정 | 규칙·비용·hurdle을 `docs/strategy_spec.md`에 동결. 허용된 변형은 §8 sensitivity grid뿐이고, `N_configs`와 함께 전부 공개 |

## "라이브 비스무리"의 범위

| 라이브와 같은 것 | 라이브와 다른 것 |
|---|---|
| 실시간 Binance 호가(`bookTicker`, `premiumIndex`)를 60초마다 폴링 | 실자금 없음. 체결은 paper(bid/ask ± 슬리피지, 시장 충격·대기열 없음) |
| 결정은 닫힌 1h 봉, 백테스트와 같은 신호 함수 | 결정은 정각 이후 첫 폴링에서 이뤄지고 그때의 호가로 체결(봉 종가가 아님) |
| 실제 정산 펀딩률로 펀딩 적립, 분기물 만기 정산 | 만기 정산가는 만기 후 첫 루프의 인덱스(Binance는 마지막 30분 평균 인덱스) |
| `book.json` 원자적 저장, append-only `events.jsonl` 원장, 프로세스 락 | 백테스트의 노출 재조정 규칙은 live에 미적용(포지션당 코인 수량 고정) |
| testnet 실주문(MARKET), 모드별 원장 분리(paper / testnet) | testnet 호가는 mainnet과 다르고, 분기물 상장 여부가 그때그때 달라 S1·S3가 `UNSUPPORTED`로 건너뛰어질 수 있음 |
| kill switch, 종료 코드별 감시자 동작(`docs/operations.md`) | VIP0 taker 수수료만 가정. 거래소 증거금 엔진은 근사 |

운영 런북: [`docs/operations.md`](docs/operations.md).

## 전략과 이론 근거

| ID | 전략 | 포지션 (target = +1) | 비교 대상 (시장 − 이론) | 근거 |
|---|---|---|---|---|
| S1 | cash-and-carry | 현물 long / 분기물 short | 분기물 연율 베이시스 `b = ln(F/S)/τ` − 공정 베이시스 `r − y` | cost of carry, Schmeling-Schrimpf-Todorov |
| S2 | funding carry | 현물 long / 무기한 short | 30일 trailing 펀딩 `f̂` − 균형 펀딩 `f* ≈ r − y` | He-Manela-Ross-von Wachter Prop. 1 |
| S3 | calendar | 무기한 long / 분기물 short (역방향 허용) | 분기물-무기한 스프레드 `ln(F/P)/τ` − `f̂` | static replication (펀딩 예측·기간 프리미엄 거래) |
| S4 | random-maturity | 현물 long / 무기한 short | 무기한 프리미엄 `ρ = κ·ln(P/S)` − `ρ*`, 무차익 밴드 `κ·ln(1 + C)` 밖에서 진입 | HMRW Prop. 2, Ackerer-Hugonnier-Jermann |

비용(spec §4): 다리당 수수료 + 슬리피지 = 현물 10 + 2 bp, 선물 5 + 2 bp(Binance VIP0 taker). 현물+선물 왕복
`C` = 38 bp, 선물+선물 = 28 bp. 진입 hurdle(연율) = `C × 365 / H + 3%`, `H`는 S1·S3는 만기까지 일수, S2는
30일. 세부 규칙: [`docs/strategy_spec.md`](docs/strategy_spec.md).

## 결과 (백테스트이며 실거래·paper 결과가 아님)

spec v1.0.2 사전등록 셀(`git tag prereg-v1.0.2`): Binance VIP0 taker 수수료(10 / 5 bp) + 다리당 슬리피지
2 bp, 다리당 10,000 USDT, 선물 다리 2배 레버리지(현물 포함 전략은 isolated, 선물 쌍은 cross),
2020-01-01 ~ 2026-10-17, 1시간 봉, 같은 봉 체결. 헤드라인은 3개월 T-bill **초과** 수치로, 포지션이 없는
날에도 전략에 배정된 자본에 T-bill을 부과합니다(대부분의 시간 쉬는 규칙은 그만큼 불리). 전체 리포트,
연도별·스트레스 구간 표, P&L 분해, sensitivity grid:
[`reports/BTCUSDT/summary.md`](reports/BTCUSDT/summary.md), [`reports/ETHUSDT/summary.md`](reports/ETHUSDT/summary.md)
(commit `36f7433`).

| | 전략 | 초과 연수익 | 초과 Sharpe | 원시 Sharpe | 최대 낙폭 | 왕복 횟수 | 포지션 보유 비율 |
|---|---|---:|---:|---:|---:|---:|---:|
| BTC | S1 cash-and-carry | 0.91% | 0.26 | 1.06 | 6.45% | 12 | 33% |
| BTC | S2 funding carry | **2.77%** | **3.14** | 6.68 | 2.00% | 5 | 45% |
| BTC | S3 calendar | −1.55% | −0.37 | 0.32 | 12.54% | 36 | 30% |
| BTC | S4 random-maturity | −1.36% | −2.24 | 2.59 | 1.32% | 8 | 2% |
| ETH | S1 cash-and-carry | 0.24% | 0.07 | 0.90 | 6.52% | 14 | 32% |
| ETH | S2 funding carry | **4.63%** | **4.03** | 6.73 | 1.25% | 4 | 52% |
| ETH | S3 calendar | −1.67% | −0.35 | 0.25 | 9.85% | 26 | 27% |
| ETH | S4 random-maturity | −0.40% | −0.44 | 2.81 | 0.78% | 10 | 3% |

문헌 재확인 관점에서 읽으면:

* **S2 funding carry**만 비용 차감 후 T-bill을 이깁니다. 다만 수익은 2020-21년(BTC 초과수익 9.3%, 19.7%)과
  2024년(0.7%)에 몰려 있고, 2022-23년과 2025-26년은 초과 기준으로 0 근처이거나 마이너스입니다. BTC 펀딩
  정산값 중 0.01%(Binance 기본 이자 성분)에 정확히 고정된 비율은 2020년 50.5%에서 2026년 6.7%로
  떨어졌습니다. Borri-Liu-Tsyvinski-Wu가 보고한 펀딩 압축이 원시 정산값에서도 보입니다.
* **S1 cash-and-carry**는 2021년과 2024년의 베이시스 급등을 잡았고(BTC 원시수익 13.8%, 8.1%) 나머지 기간은
  거의 쉬었습니다. 자본이 표본의 2/3 동안 놀기 때문에 초과 수치는 작습니다.
* **S3 calendar**는 비용 차감 후 손실입니다. 과거 실현 펀딩은 분기물 베이시스를 잘 예측하지 못하고, 36회
  × 28 bp 왕복 비용이 기간 프리미엄을 다 먹습니다.
* **S4 random-maturity(HMRW)**는 소매 taker 수수료에서는 거의 거래하지 않습니다. HMRW 논문의 수수료
  수준(재현 셀)에서는 39-157회 거래하고 연 +1.4% ~ +3.3% 초과수익을 냅니다. "이 거래는 수수료가 결정한다"는
  HMRW의 결론을 재현한 것입니다.
* 2배 레버리지, 종가 기준으로 표본 내 청산 없음(최대 증거금 사용률 0.56-0.68). sensitivity grid
  (`N_configs` = 전략당 12 + 재현 셀 3)에서 사전등록 셀은 최고 셀이 아닙니다.

## 누가 베이시스를 가져가나: 비용과 지연

sanctioned grid에 이미 있던 셀만 써서 만든 백테스트 진단입니다(규칙·임계·비용 가정 변경 없음, `N_configs`
그대로): [`reports/cost_latency/summary.md`](reports/cost_latency/summary.md) (commit `454937d`).

![왕복 비용 대비 초과 수익](reports/cost_latency/fee_curve.png)

* **단기 괴리 거래는 비용이 결정합니다.** HMRW의 random-maturity 규칙(S4)은 소매 taker 왕복 비용 38 bp에서
  T-bill에 집니다(BTC / ETH 왕복 8 / 10회, 초과 −1.36% / −0.40%). HMRW 논문의 수수료 수준(왕복 4.9-16.4 bp)
  에서는 같은 규칙이 39-157 / 59-255회 거래하고 연 +1.37% ~ +3.29% / +3.28% ~ +5.22% 초과 수익을 냅니다.
  신호는 같고, 스프레드를 건너는 비용이 수익 여부를 정합니다.
* **이 셀들은 수수료만 낮춘 것이지 체결 위험까지 낮춘 것이 아닙니다.** 여전히 1시간 종가 taker 체결을
  가정합니다. 유동성 공급자처럼 지정가 호가로 그 수수료를 얻으면 체결 불확실성, 대기열 위치, 역선택이
  추가되는데, 이 백테스트는 그것을 측정하지 않습니다(아래 다음 단계).
* **지연.** 한 봉(1시간) 늦게 체결하면 S1의 엣지가 사라지고(BTC +0.91% → −0.65%, ETH +0.24% → −0.60%), S2는
  그대로입니다(입력값이 펀딩 정산 시각에만 바뀜). BTC는 S1 지연 손실 전체가 극단 봉 3개(새 분기물의 첫
  거래 시간대, 2021-04-18 분기물 wick, 2021-09-07 급락)에서 나왔고, ETH는 더 고르게 퍼져 있습니다. 늦은
  taker는 정확히 같은 봉 종가 체결을 가장 믿기 어려운 봉에서 손해를 봅니다.
* **수수료 우위는 필요조건이지 충분조건이 아닙니다.** 2026년(연초 이후)에는 HMRW 재현 셀을 포함한 어떤
  sanctioned 셀도 두 코인 모두에서 T-bill을 이기지 못했습니다. S4는 HMRW 수수료에서도 거래가 0건입니다.
  2026년에 무기한이 현물보다 높게 마감한 시간은 0.4% / 0.1%(괴리 중앙값 −4.7 / −4.8 bp)뿐이었고, S4는
  프리미엄을 파는 방향으로만 거래할 수 있습니다.

다음 단계(아직 실행 안 함, 결과 전에 사전등록 필요, `docs/research_log.md`): S4를 HMRW 공정 프리미엄 주변의
지정가 호가로 바꾸고, 1분 데이터에서 가격이 호가를 관통할 때만 체결로 보고, 체결 후 markout으로 역선택을
측정합니다. 평가는 이미 본 표본이 아니라 paper 데몬이 앞으로 기록하는 bid/ask 스냅샷으로 합니다.

## 최근에 S1·S2 거래가 없는 이유

리포트의 연도별 표에서 2026년 S1·S2는 BTC·ETH 모두 왕복 0회, 포지션 보유 0%입니다. 마지막 진입은
2024-10 ~ 11월, 마지막 청산은 2025-02 ~ 03월입니다(`reports/*/trades_*.csv`). sensitivity grid의 모든
셀로 넓혀도 2026년 초과 수익이 양수인 셀은 없습니다(`reports/cost_latency/summary.md` §4). 버그가
아니라 규칙이 의도대로 동작한 결과이고, spec 공식만으로 설명됩니다.

* **S2**: hurdle = 38 bp × 365 / 30 + 3% = 연 7.62%. 진입하려면 `f̂ > r + 7.62%`여야 합니다. Binance 펀딩은
  프리미엄 지수가 clamp 범위(이자 성분 ± 0.05%) 안에 있으면 8시간마다 0.01%(연 10.95%)로 고정됩니다.
  따라서 **T-bill이 연 3.33%를 넘으면 펀딩이 기본값에 내내 고정돼 있어도 진입할 수 없고**, 프리미엄이
  clamp 범위 위로 올라갈 만큼 강한 레버리지 매수 수요가 있어야만 진입합니다. 금리가 0 근처였던 2020-21년에는
  기본 펀딩만으로도 충분했습니다. 2026년에는 0.01%를 넘는 정산값이 없고 대부분이 그보다 낮습니다(패널의
  `funding_rate` 기준).
* **S1**: hurdle = 38 bp × 365 / 만기일수 + 3%(만기 90일이면 연 4.54%). 진입하려면 분기물 연율 베이시스가
  `r + hurdle`을 넘어야 하는데, 2026년 분기물 베이시스는 T-bill 수준이거나 그보다 낮습니다.
* 거래를 만들어내려고 규칙을 바꾸는 것은 사전등록 위반입니다(spec §5 "Explicitly not done"). 비용을
  넘지 못하는 carry에 들어가지 않는 것이 이 규칙의 목적입니다.

## 구성

```
src/basis_arb/
  config.py           상수, 비용·증거금 모델, 전략 파라미터 (동결된 기본값)
  types.py            패널 스키마 계약 (validate_panel)
  model/fair_value.py cost of carry, HMRW 무기한 공정가, 무차익 밴드, Binance 펀딩 공식
  model/signals.py    enrich() + 사전등록된 상태 기계 4개 -> target 프레임
  data/               Binance + FRED 수집기(키 불필요한 공개 엔드포인트), parquet 캐시, 패널 빌더
  backtest/           엔진, 지표, 리포트, sensitivity grid
  live/               history, feed, state, broker (Paper / Testnet), daemon
scripts/              비용·지연 진단 리포트 (cost_latency_report.py -> reports/cost_latency/)
tests/                오프라인 단위 테스트 197개 (synthetic 패널 생성기: conftest.make_panel)
docs/                 strategy_spec, theory, data_sources, operations, research_log, NEXT
```

## 빠른 시작

```bash
uv venv .venv --python 3.11
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"     # Windows; 다른 OS는 .venv/bin/python
.venv/Scripts/python.exe -m pytest -q -m "not network"
.venv/Scripts/python.exe -m basis_arb.data.panel --root BTCUSDT --start 2020-01-01
.venv/Scripts/python.exe -m basis_arb.backtest.run --root BTCUSDT --panel data/panel_BTCUSDT_1h.parquet --out reports/BTCUSDT
.venv/Scripts/python.exe -m basis_arb.live.daemon --root BTCUSDT --mode paper --once
```

## 정직성과 한계

* 모든 수치는 백테스트, paper, testnet 중 무엇인지 표시합니다. 이 저장소에 실자금 성과는 없습니다.
* `src/basis_arb/live/broker.py`에는 mainnet 거래 호스트가 없고(단위 테스트로 확인), testnet 키는
  환경변수에서만 읽습니다.
* 알려진 단순화: 단일 거래소, VIP0 taker 수수료, 1시간 봉 종가 체결, 만기 정산가를 만기 시점 현물 종가로
  근사, ETH 현물은 스테이킹 없음, 스테이블코인 조달 금리를 T-bill로 대체, 거래소 증거금 엔진을 isolated
  청산으로 근사, 거래소 파산 위험(FTX형) 모델 없음.
* Schmeling-Schrimpf-Todorov(BIS WP 1087)가 보고한 carry는 위험 프리미엄이고 2024년 이후 압축됐습니다
  (Borri-Liu-Tsyvinski-Wu, arXiv:2510.14435). 그래서 결과를 연도별·스트레스 구간별로 함께 보고합니다.

## 만든 방식

저자가 AI 코딩 에이전트(Claude Code)를 지휘했고 모든 커밋에 공동 작성자 표기가 있습니다. 저자는 범위를
정하고, 전체 표본 실행 전에 전략 규칙을 동결했으며(`git tag prereg-v1.0.2`), 이론과 코드의 대응을 논문과
대조했고, 독립 리뷰 두 번(코드 수준, 엔지니어링 수준)을 받아 그 결과를 반영한 뒤 인용하는 실행을
돌렸습니다. spec과 테스트가 계약입니다. LLM 출력은 결정·주문 경로에 들어가지 않습니다(데몬은 결정론적 코드).

## 라이선스

MIT.
