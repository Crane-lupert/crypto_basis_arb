# Theory notes: what the model implements and where it comes from

These notes are the reading record behind `src/basis_arb/model/fair_value.py`. Each anchor was checked
on 2026-10-18 against the publisher/arXiv page (metadata, abstract, and the specific propositions used).
Nothing below is cited from memory alone.

## 1. Dated futures: cost of carry and the crypto "carry"

For an asset with spot price `S`, financing rate `r` (USD / stablecoin), and a yield on holding the
asset `y` (zero for BTC, a staking proxy for ETH), the no-arbitrage forward price with no storage cost is

    F*(t, T) = S_t · exp((r − y) · (T − t)).

Define the (continuously compounded) annualized basis `b = ln(F/S)/τ`. The fair basis is `b* = r − y`;
the **excess basis** `e_fut = b − b*` is what the cash-and-carry trade earns on top of financing.

Schmeling, Schrimpf and Todorov, *Crypto carry* (BIS Working Paper 1087, April 2023; CEPR DP 20719, 2025)
document, for BTC and ETH futures, that this carry averages above 10% per year and reaches up to 60% per
year; that interest-rate differentials explain very little of its variation; that it behaves like a
volatile convenience yield driven by leveraged, trend-chasing demand from smaller investors and by scarce
arbitrage capital; that high carry predicts subsequent price crashes; and that cash-and-carry positions are
risky in practice because of margin spikes and liquidations during drawdowns.
Implications adopted here: the hurdle carries an explicit buffer above costs (the basis is a risk premium,
not a free lunch); the engine models the short leg's margin and liquidation; the report shows the carry
alongside the stress episodes rather than only the full-sample Sharpe.
Sources: https://www.bis.org/publ/work1087.htm · https://www.bis.org/publ/work1087.pdf

**Decay evidence.** Borri, Liu, Tsyvinski and Wu, *Cryptocurrency as an Investable Asset Class: Coming of
Age* (arXiv:2510.14435, v4 March 2026), Section 3.6 ("When the funding dries up, we finally learn the worth
of futures"): a Binance BTC perpetual carry strategy (long spot, short perpetual, August 2020 to May 2025)
has a full-sample annualized Sharpe ratio of 6.45, falls to 4.06 beginning in 2024, and turns negative in
2025, with profits "mostly driven by the funding rate". This is why strategy S2 compares trailing funding
with the *current* equilibrium `r − r′` instead of a fixed threshold, and why the report breaks results out
by year and by the 2026 funding-compression episode rather than quoting a full-sample number.
Source: https://arxiv.org/abs/2510.14435

**Segmented margin.** Mallory, *Implied ETF Carry Rates and the Limits of Arbitrage in Segmented Bitcoin
Markets* (arXiv:2605.29309, May 2026) finds the carry implied by IBIT options differs from matched CME
futures carry by about 2.5 percentage points on average and attributes the gap to frictions that limit
cross-margining. The same friction is why this repository models the futures leg's margin separately from
the spot leg (isolated mode) and only optionally as cross-collateral.
Source: https://arxiv.org/abs/2605.29309

## 2. Perpetual futures: no-arbitrage pricing with funding

### 2.1 He, Manela, Ross and von Wachter, *Fundamentals of Perpetual Futures*

arXiv:2212.06888 (first version December 2022; v7 September 2026). The perpetual pays funding
continuously at rate `κ (F_s − S_s)` per unit time, with `κ = 1095` per year so that the cumulative
payment over 8 hours matches one exchange funding interval. With a constant stablecoin short rate `r`,
a constant underlying rate `r′`, and `κ > r − r′`:

* **Proposition 1 (frictionless):** `F_t = κ / (κ − (r − r′)) · S_t`. The perpetual trades at a
  constant, tiny multiple of spot; the equilibrium funding a short collects per period is
  `(F − S)/S = (r − r′)/(κ − (r − r′)) ≈ (r − r′)/κ`, i.e. annualized funding ≈ `r − r′`.
* **Proposition 2 (round-trip trading cost C):** `|F_t − κ/(κ − (r − r′)) · S_t| ≤ C`. In annualized
  premium units `ρ = κ · ln(F/S)` the band is `[κ · ln(1 − C), κ · ln(1 + C)]`.
* **Random-maturity arbitrage:** open a spot-vs-perpetual position when the deviation `ρ` leaves the
  cost-implied band, close when it first returns to zero. The holding period is random (hence the name).
  Their Table 5 reports Sharpe ratios on Binance hourly data (2020 to 2024-03-11): BTC 3.53 (no fees),
  2.20 / 2.16 / 1.80 at low / medium / high fee tiers; ETH 4.83 → 2.55; BNB, DOGE and ADA higher.
  The high fee tier is 6.75 bp spot and 1.44 bp futures; their risk-free rate is the average Aave
  stablecoin supply/borrow rate.
* **Empirics:** deviations of crypto perpetuals from the benchmark are far larger than deviations in
  traditional currency markets, co-move across coins, and shrink as the market matures.

What this repository takes from it: `perp_fair_price`, `fair_funding_per_period`, `fair_perp_premium_ann`,
`no_arb_band_ann`, and strategy S4 (`random_maturity`), which follows the open/close rule above with the
repository's own (higher, VIP0) cost tier. S2 (`funding_carry`) uses the same equilibrium: trailing
realized funding is compared with `r − r′`, not with zero and not with the exchange's fixed 0.01%.
Sources: https://arxiv.org/abs/2212.06888 · https://arxiv.org/html/2212.06888v5

### 2.2 Ackerer, Hugonnier and Jermann, *Perpetual Futures Pricing*

arXiv:2310.11771 (v2 September 2024); NBER Working Paper 32936 (2024); *Mathematical Finance*
(DOI 10.1111/mafi.70018). They derive closed-form no-arbitrage prices for linear, inverse and quanto
perpetuals in discrete and continuous time and show that the perpetual price equals the risk-neutral
expected spot price at a *random* future date whose distribution reflects how strongly funding anchors the
contract to spot; some funding designs make futures and spot coincide, and the contract can be replicated
by dynamic trading in the underlying.

For the continuous-time linear-funding case the random date is exponential with rate `κ`, so
`F_t = κ ∫_0^∞ e^{−κ s} E_t^Q[S_{t+s}] ds`: the perpetual is a `κ e^{−κs}`-weighted average of dated
futures prices. `perp_price_random_maturity` evaluates this integral numerically, and the unit tests
check that with `E^Q[S_{t+s}] = S e^{(r − r′) s}` it reproduces Proposition 1 above. The same
representation motivates strategy S3 (`calendar`): under no-arbitrage the quarterly basis and the
expected cumulative funding over the same horizon are both the cost of a synthetic long, so their
difference is a mispricing between the two futures.
Sources: https://arxiv.org/abs/2310.11771 · https://www.nber.org/papers/w32936 ·
https://infoscience.epfl.ch/handle/20.500.14299/256201

### 2.3 Angeris, Chitra, Evans and Lorig, *A Primer on Perpetuals*

arXiv:2209.03307 (September 2022); *SIAM Journal on Financial Mathematics*, Short Communication
(DOI 10.1137/22M1520931). In a continuous-time, frictionless market they study two designs, one where
funding flows from longs to shorts and one where a time-varying discount factor replaces funding; with
continuous, strictly positive prices they obtain model-free expressions for the funding and discount
rates, discuss replication of the short side when prices jump (volatility independent of the underlying),
and relate perpetuals to variance swaps and leveraged ETFs. Used here only as the replication viewpoint:
funding is the carry cost of the dynamically rolled position that replicates the perpetual.
Sources: https://arxiv.org/abs/2209.03307 · https://epubs.siam.org/doi/10.1137/22M1520931

## 3. Exchange mechanics that the model must respect

Binance USDⓈ-M funding (support article 360033525031):

    premium index P   = [max(0, impact bid − index) − max(0, index − impact ask)] / index, every 5 s
    funding rate F    = [ P̄ + clamp(I − P̄, −0.05%, +0.05%) ] / (8 / N),   I = 0.01% per 8h (0.03%/day)
    cap / floor       = ± 0.75 × maintenance margin ratio at max leverage  (BTC, ETH: ± 0.30%)
    funding amount    = mark-price notional × F, paid only by positions open at the stamp

`P̄` is a time-weighted average over the interval (weights 1..n). The interest component `I` is the
exchange's assumed USDT-minus-coin rate differential; the theory in §2 replaces this fixed 0.01% with the
observed `(r − r′)/κ`. Since 2025-05 a contract moves to 1-hour settlement after a capped print and
(since 2026-01) back to 4-hour after 16 calm prints; the panel therefore keeps the *actual* stamps from the
funding-history endpoint rather than assuming 8 hours.

Quarterly delivery contracts (`ROOT_YYMMDD`) expire at 08:00 UTC on the last Friday of March, June,
September and December. The settlement price is the average of the index over the final 30 minutes
(final hour for contracts listed before November 2024); a delivery fee is charged at the taker rate on the
settled notional; new positions are blocked in the final 10 minutes. The engine settles at the spot close
of the expiry bar as a proxy and charges the delivery fee.

## 4. Inputs for `r` and `r′`

* `r`: 3-month US T-bill (FRED `DTB3`, discount basis, keyless CSV). HMRW use Aave stablecoin rates,
  which track T-bills loosely but are not available keyless with history. The difference (a stablecoin
  spread of a few percent in 2021, near zero in 2024-26) is an acknowledged source of error in `b*`.
* `r′ = y`: 0 for BTC. For ETH a constant 3% staking proxy is used; the true staking yield drifted from
  about 4-5% (2022-23) to about 3% (2025-26), so a time series would be a strict improvement.

## 5. Related work consulted but not used as an anchor

* Kim and Park, *Designing funding rates for perpetual futures in cryptocurrency markets*
  (arXiv:2506.08573, June 2025): BSDE-based funding design so that the perpetual tracks a target value;
  tangential to trading, relevant to why exchanges tune the interest component and caps.
* Binance support articles 360033525031 (funding) and the quarterly delivery notices (settlement window,
  delivery fee) are the only non-academic sources, used for mechanics in §3.
