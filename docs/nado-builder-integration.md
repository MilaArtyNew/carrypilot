# Nado Builder Integration Plan

This document describes how CarryPilot plans to integrate with Nado's Builder Code system.

CarryPilot is a Telegram-based assistant for semi-automated funding-rate arbitrage between perpetual futures venues. Nado is planned as a core execution venue in the cross-venue funding workflow.

## Goal

The goal of the Builder Code integration is to route eligible Nado orders through CarryPilot's registered builder profile and include builder attribution in the order appendix.

This allows CarryPilot to:

- identify Nado as the execution venue for approved trades;
- include a registered `builder_id` in supported orders;
- apply a configurable builder fee rate;
- keep the fee low enough to preserve funding-arbitrage edge;
- support future fee attribution and reporting.

## Current status

- CarryPilot MVP is implemented.
- Telegram approval flow is implemented.
- Paper trading mode is implemented.
- Nado exchange adapter exists in the codebase.
- Nado Builder Program application has been submitted.
- Builder Code integration is pending Nado builder registration and builder ID issuance.

## Nado Builder Code overview

According to the Nado Builder Integration documentation, builder attribution is included in the order `appendix`.

Relevant fields:

- `builder` / `builder_id`
  - 16-bit identifier.
  - Bits `48-63` in the appendix.
  - `0` means no builder.
- `builder_fee_rate`
  - 10-bit value.
  - Bits `38-47` in the appendix.
  - Denominated in `0.1 bps` units.

Fee unit conversion:

- `1` unit = `0.1 bps`
- `10` units = `1 bps`
- `50` units = `5 bps`
- `100` units = `10 bps`

## Planned fee policy

CarryPilot is designed for funding-rate opportunities, where edge is sensitive to fees, bid/ask spread, and slippage.

Planned fee range:

- Minimum builder fee: `0 bps`
  - Builder fee units: `0`
- Maximum builder fee: `5 bps`
  - Builder fee units: `50`

Initial testing should use `0 bps` or the lowest fee approved by Nado. Higher fees should only be considered after live validation and only if they do not materially harm execution quality.

## Appendix construction plan

CarryPilot should build the Nado order appendix with the normal order fields plus builder-specific fields.

Python reference implementation:

```python
def build_appendix_with_builder(
    order_type: int = 0,
    reduce_only: bool = False,
    isolated: bool = False,
    isolated_margin: int = 0,
    trigger_type: int = 0,
    builder_id: int = 0,
    builder_fee_rate: int = 0,  # in 0.1 bps units
) -> int:
    appendix = 0

    # Version: bits 0-7
    appendix |= 1

    # Isolated: bit 8
    if isolated:
        appendix |= 1 << 8

    # Order type: bits 9-10
    appendix |= (order_type & 0b11) << 9

    # Reduce only: bit 11
    if reduce_only:
        appendix |= 1 << 11

    # Trigger type: bits 12-13
    appendix |= (trigger_type & 0b11) << 12

    # Builder fee rate: bits 38-47
    appendix |= (builder_fee_rate & 0x3FF) << 38

    # Builder ID: bits 48-63
    appendix |= (builder_id & 0xFFFF) << 48

    # Isolated margin value: bits 64-127
    if isolated and isolated_margin > 0:
        appendix |= (isolated_margin & ((1 << 64) - 1)) << 64

    return appendix
```

Example:

```python
# Example: route through builder ID 123 with 1 bps builder fee
appendix = build_appendix_with_builder(
    order_type=0,
    builder_id=123,
    builder_fee_rate=10,  # 10 units = 1 bps
)
```

## Required configuration

CarryPilot should expose the following environment variables after Nado confirms builder registration:

```env
NADO_BUILDER_ID=0
NADO_BUILDER_FEE_RATE=0
```

Recommended behavior:

- If `NADO_BUILDER_ID` is missing or `0`, do not include builder attribution.
- If `NADO_BUILDER_ID=0`, force `NADO_BUILDER_FEE_RATE=0`.
- Validate that `NADO_BUILDER_FEE_RATE` is within Nado-approved bounds before placing live orders.
- Log the effective builder settings at startup, without exposing private keys.

## Validation rules

Before placing a live order with builder attribution, CarryPilot should validate:

- `builder_id` is registered and provided by Nado.
- `builder_fee_rate` is within Nado-approved minimum and maximum bounds.
- `builder_fee_rate` is `0` when `builder_id` is `0`.
- The final appendix value matches the expected bit layout.
- The order remains valid after the normal pre-trade re-check.

## CarryPilot execution flow with builder attribution

1. Scanner detects a cross-venue funding-rate opportunity.
2. Signal is sent to Telegram.
3. User clicks `Approve`.
4. CarryPilot re-checks:
   - funding spread;
   - mark prices;
   - bid/ask spread;
   - time to funding;
   - balances;
   - existing open positions.
5. If Nado is one of the execution venues, CarryPilot builds the Nado order appendix with builder attribution.
6. CarryPilot submits both legs.
7. If one leg fails, emergency-close logic is triggered.
8. Position tracking and risk monitoring continue after entry.

## Risk considerations

Builder fees must not make a funding opportunity uneconomic.

CarryPilot should account for builder fees in net-profit estimation before showing a trade as actionable. The scanner should avoid signals where the estimated net profit becomes too small after:

- exchange taker fees;
- bid/ask spread;
- builder fee;
- expected slippage;
- operational safety buffer.

## Nado support requested

The most useful support from Nado:

- Builder registration for CarryPilot.
- Builder ID issuance.
- Confirmation of fee bounds.
- Confirmation that the appendix implementation matches production requirements.
- Reliable API/data access for:
  - funding rates;
  - quotes/orderbook;
  - balances;
  - order placement;
  - position state.
- Guidance on testing builder-attributed orders safely.
- Fee rebates or incentives during early testing.

## Implementation checklist

- [ ] Receive registered `builder_id` from Nado.
- [ ] Confirm approved builder fee bounds.
- [ ] Add `NADO_BUILDER_ID` and `NADO_BUILDER_FEE_RATE` to `.env.example`.
- [ ] Add appendix builder helper to the Nado exchange adapter.
- [ ] Include builder fee in net-profit calculations.
- [ ] Add startup validation for builder config.
- [ ] Add logging for effective builder settings.
- [ ] Test appendix construction with known values.
- [ ] Test paper-mode behavior without sending live orders.
- [ ] Test live mode with minimal size only after Nado confirms setup.

## Repository

CarryPilot repository:

https://github.com/milanewgpt/carrypilot
