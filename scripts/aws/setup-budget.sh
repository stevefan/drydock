#!/usr/bin/env bash
# Create the $25/month account-level budget with email alerts at 50%, 100%,
# and forecasted-100%. Idempotent: updates in place if the budget already
# exists.
#
# Runs as the `personal` profile (account owner's perspective).

set -euo pipefail

ACCOUNT_ID=047535447308
BUDGET_NAME=drydock-cost-cap
BOOTSTRAP_PROFILE=personal
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

aws_p() { aws --profile "$BOOTSTRAP_PROFILE" "$@"; }

echo "==> Creating/updating budget ${BUDGET_NAME}..."
if aws_p budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" >/dev/null 2>&1; then
  echo "    budget exists; updating amount + cost types"
  aws_p budgets update-budget \
    --account-id "$ACCOUNT_ID" \
    --new-budget "file://${HERE}/budget.json"
  echo "    (notifications preserved on update; use recreate if you need to change them)"
else
  aws_p budgets create-budget \
    --account-id "$ACCOUNT_ID" \
    --budget "file://${HERE}/budget.json" \
    --notifications-with-subscribers "file://${HERE}/budget-notifications.json"
fi

echo "==> Budget state:"
aws_p budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" \
  --query 'Budget.{Name:BudgetName,Limit:BudgetLimit,Spent:CalculatedSpend.ActualSpend}'

echo "==> Notifications:"
aws_p budgets describe-notifications-for-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" \
  --query 'Notifications[].{Type:NotificationType,Op:ComparisonOperator,Threshold:Threshold,Unit:ThresholdType}'
