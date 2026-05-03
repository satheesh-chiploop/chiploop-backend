# Phase 8E: Demo-to-Trial Flow

## Product Rule

ChipLoop separates low-friction demo access from the 7-day trial.

- Demo: email signup only, no credit card.
- Trial: credit card required through Stripe, no charge during the 7-day trial.
- Paid conversion: trial converts to Starter unless canceled.

## Demo Limit

The guided Arch2RTL demo is limited to 3 server-counted runs per user.

- Runs 1-3: allow the prefilled PWM Arch2RTL demo and return a soft trial CTA.
- Run 4: block before starting the workflow and ask the user to start the 7-day trial.

The backend only treats the request as demo-eligible when it matches the guided PWM payload:

- project: `pwm_controller_onboarding`
- top module: `pwm_controller`
- language: `systemverilog`
- known PWM spec markers
- packaging and UPF-lite enabled

Custom Arch2RTL specs still require checkout/trial.

## Persistence

No SQL migration is required. Demo run count is stored in the existing onboarding metadata:

```json
{
  "arch2rtl_demo_runs": 1,
  "arch2rtl_last_demo_run_at": "..."
}
```

The `/settings/onboarding` response includes demo usage under `onboarding.demo.arch2rtl`.

## Frontend Behavior

- Apps onboarding explains demo mode is no-credit-card and limited to 3 runs.
- After each allowed demo run, Arch2RTL shows a friendly 7-day trial CTA.
- If the backend returns `trial_checkout_required`, Arch2RTL shows a blocking trial prompt instead of a raw error.

## Safety

- Enforcement is backend-side.
- Existing paid/trial users are unaffected.
- Other app workflow runs still require checkout through the existing middleware.
- No secrets, Stripe keys, or frontend-only trust are introduced.
