# Balance & Reward System — Architecture Report

> **Status**: Analysis & Design (NO implementation yet)
> **Date**: 2026-09-20
> **Phase**: 🏠 الرئيسية (Home)

---

## Table of Contents

1. [Existing State](#1-existing-state)
2. [Existing Reward Flow](#2-existing-reward-flow)
3. [Existing User State](#3-existing-user-state)
4. [Search Findings](#4-search-findings)
5. [Proposed Architecture](#5-proposed-architecture)
6. [Data Model Proposal](#6-data-model-proposal)
7. [Precision Strategy](#7-precision-strategy)
8. [Idempotency Strategy](#8-idempotency-strategy)
9. [Completion / Reward Boundary](#9-completion--reward-boundary)
10. [Home Integration](#10-home-integration)
11. [Future Compatibility](#11-future-compatibility)
12. [Open Product Decisions](#12-open-product-decisions)
13. [Explicit Non-Implementation List](#13-explicit-non-implementation-list)

---

## 1. Existing State

### 1.1 Technology Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python 3.12, python-telegram-bot 21.10 |
| Database | SQLite (WAL mode, foreign keys ON) |
| Frontend | Vanilla JS Mini App (Telegram WebApp API) |
| Server | Flask (WispByte deployment) |
| Testing | pytest 8.3.4 + unittest |

### 1.2 Database Schema (Current)

```sql
-- Users table
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    referred_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (referred_by) REFERENCES users(user_id)
);

-- Tasks table (reward is INTEGER)
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    type TEXT NOT NULL,
    reward INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    task_data TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- User task state
CREATE TABLE user_tasks (
    user_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    PRIMARY KEY (user_id, task_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
```

### 1.3 Approved Home-Phase Commits

| Commit | Description |
|--------|-------------|
| `c68bed6` | Mini App Shell |
| `77addca` | Home UI |
| `8eb6877` | Add Task CTA |
| `931ccfd` | WispByte serving |
| `3aa17a4` | Telegram Profile integration |

---

## 2. Existing Reward Flow

### 2.1 Task Lifecycle Pipeline

```
TaskStartGate.start(user_id, task_id)
    │  Validates: user exists, task exists, task active, state = available
    │  Mutates: user_tasks status = started
    ▼
TaskAttemptPolicy.can_submit(user_id, task_id)
    │  Read-only: validates user/task exist, task active, state = started
    ▼
TaskSubmissionService.submit(user_id, task_id, actual_data)
    │  Builds: VerificationContext (frozen/immutable)
    │  Delegates to: TaskVerifier.verify(context)
    │  Returns: VerificationResult(PASSED | FAILED | ERROR)
    │  Does NOT complete the task
    ▼
CompletionBridge.complete_after_verification(user_id, task_id, actual_data)
    │  Orchestrates: policy → submission → (only if PASSED) → completion
    ▼
CompletionGate.complete(user_id, task_id, verification_result)
    │  Validates: verification passed, user/task exist, state = started
    │  Mutates: user_tasks status = completed
    │  Does NOT grant rewards
    ▼
COMPLETED (no reward credited)
```

### 2.2 Reward Field in Database

- **Type**: `INTEGER NOT NULL`
- **Validation**: `reward >= 0` (negative rejected)
- **Exposure**: `TaskSummary.reward: int` (read-only, for catalog display)
- **Forbidden**: `"reward"` is in `FORBIDDEN_FIELDS` for submission — client cannot alter task reward via submission data
- **NOT used for crediting**: No code path reads `task.reward` to credit a user balance

### 2.3 Key Observation

The reward field exists as **metadata only**. It is:
- ✅ Created via `db.create_task(reward=N)`
- ✅ Stored in the `tasks` table
- ✅ Read by `db.get_task()` and `db.list_tasks()`
- ✅ Exposed via `TaskSummary.reward`
- ❌ **Never credited to any user on completion**
- ❌ **No balance table exists**
- ❌ **No transaction/ledger table exists**

---

## 3. Existing User State

### 3.1 Users Table

| Column | Type | Notes |
|--------|------|-------|
| `user_id` | INTEGER PK | Telegram user ID |
| `username` | TEXT | Telegram @username |
| `first_name` | TEXT | Telegram display name |
| `referred_by` | INTEGER FK | Referral attribution |
| `created_at` | TIMESTAMP | Registration time |

### 3.2 What Does NOT Exist

- ❌ No `balance` column on `users`
- ❌ No `wallets` table
- ❌ No `transactions` or `ledger` table
- ❌ No `earnings` table
- ❌ No `points` table
- ❌ No `currency` configuration
- ❌ No withdrawal/deposit tables

### 3.3 Referral System

- `users.referred_by` tracks who referred the user
- `get_referrer(user_id)` and `get_referral_count(user_id)` exist
- **No referral reward logic is implemented** — the attribution data exists but no crediting

---

## 4. Search Findings

### 4.1 Keyword Search Results

| Keyword | Found in Code | Actual Implementation |
|---------|--------------|----------------------|
| `balance` | CSS class names, HTML labels, test assertions | **Placeholder UI only** — `balance-empty` class renders "—" (em dash) |
| `wallet` | Module docstring comments (NOT responsible for wallets) | **No implementation** |
| `earnings` | None | **Not found** |
| `reward` | `tasks.reward` field, `FORBIDDEN_FIELDS`, docstrings | **Metadata only** — no crediting logic |
| `credit` | None | **Not found** |
| `debit` | None | **Not found** |
| `transaction` | None | **Not found** |
| `ledger` | None | **Not found** |
| `withdrawal` | Header button "السحب" (placeholder) | **Placeholder UI** — `showPlaceholder('السحب')` |
| `deposit` | Header button "الشحن" (placeholder) | **Placeholder UI** — `showPlaceholder('الشحن')` |
| `points` | None | **Not found** |
| `currency` | None | **Not found** |

### 4.2 UI Placeholders

**Home Balance Section** (`miniapp/js/home.js`):
```html
<div class="balance-item" data-testid="balance-reward">
    <span class="balance-label">المكافآت</span>
    <span class="balance-value balance-empty">—</span>
</div>
<div class="balance-item" data-testid="balance-available">
    <span class="balance-label">المتاح</span>
    <span class="balance-value balance-empty">—</span>
</div>
```

**Header Actions** (`miniapp/index.html`):
```html
<button id="btn-charge" class="header-btn" data-action="charge">الشحن</button>
<button id="btn-withdraw" class="header-btn" data-action="withdraw">السحب</button>
```

Both header buttons call `showPlaceholder()` — no backend wiring.

### 4.3 Test Guardrails (Existing)

Multiple test files assert that reward/balance logic does NOT exist where it shouldn't:

- `test_task_completion.py::test_no_reward_granted` — asserts no reward records after completion
- `test_task_lifecycle.py::test_no_reward_logic` — asserts `TaskLifecycle` source has no `reward` keyword
- `test_task_submission.py::test_reward_field_rejected` — asserts client cannot inject `reward` in submission
- `test_subscription.py::test_no_reward_deduction_code` — asserts subscription module has no reward/balance logic
- `test_miniapp_home.py::test_no_hardcoded_balance_numbers` — asserts no fake currency amounts in UI
- `test_miniapp_home.py::test_no_fake_reward_values` — asserts no invented reward values

**These guardrails must be preserved or updated (not broken) when the balance system is implemented.**

---

## 5. Proposed Architecture

### 5.1 Design Principles

1. **Single source of truth**: Balance is derived from the transaction ledger, never stored redundantly (or if cached, always reconcilable)
2. **Atomic crediting**: Reward credit happens inside a single SQLite transaction at the exact completion boundary
3. **Idempotent**: Replaying the same completion cannot double-credit
4. **Append-only ledger**: All balance mutations are recorded as immutable transaction rows
5. **Separation of concerns**: The existing lifecycle pipeline is NOT modified; a new `RewardService` is called AFTER `CompletionGate`

### 5.2 Architectural Layers

```
Existing Pipeline (unchanged):
    TaskStartGate → TaskAttemptPolicy → TaskSubmissionService
    → TaskVerifier → CompletionBridge → CompletionGate
    → user_tasks.status = completed

New Layer (added AFTER completion):
    CompletionGate.complete() returns True
        ↓
    RewardService.credit_task_reward(user_id, task_id)
        ↓
    ├── Read task.reward (INTEGER)
    ├── Create transaction row (idempotent)
    └── (Optionally) update cached balance column
```

### 5.3 Where Reward Credit Occurs

**The single authoritative credit point**: Inside `CompletionGate.complete()` or immediately after it, wrapped in the same SQLite transaction that transitions `user_tasks.status` to `completed`.

Recommended approach — **extend `CompletionGate` to accept an optional `on_complete` callback**:

```python
class CompletionGate:
    def complete(self, user_id, task_id, verification, on_complete=None):
        # ... existing validation ...
        db.update_user_task_status(user_id, task_id, COMPLETED, _allow_completion=True)

        if on_complete is not None:
            on_complete(user_id, task_id)

        return True
```

The caller (e.g., `CompletionBridge` or `TaskLifecycle`) provides the `on_complete` callback that invokes `RewardService`. This keeps `CompletionGate` itself reward-unaware while guaranteeing the credit happens **inside the same database transaction**.

### 5.4 Alternative: Post-Completion Hook

If modifying `CompletionGate` is undesirable, the credit can occur in `CompletionBridge.complete_after_verification()` **immediately after** `gate.complete()` returns, within the same `get_connection()` transaction context. This is simpler but couples the bridge to reward logic.

**Recommendation**: Use the callback approach for clean separation.

---

## 6. Data Model Proposal

### 6.1 New Tables

```sql
-- Immutable transaction ledger (append-only)
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,           -- 'task_reward', 'referral_reward', 'withdrawal', 'deposit', ...
    amount INTEGER NOT NULL,      -- positive = credit, negative = debit
    reference_type TEXT,          -- 'task', 'referral', 'withdrawal_request', ...
    reference_id INTEGER,         -- task_id, referral pair, withdrawal request id, ...
    idempotency_key TEXT UNIQUE,  -- e.g., f"task_reward:{user_id}:{task_id}"
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Index for fast balance computation
CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_idempotency ON transactions(idempotency_key);
```

### 6.2 Balance Computation

Balance is computed from the ledger:

```sql
SELECT COALESCE(SUM(amount), 0) AS balance
FROM transactions
WHERE user_id = ?
```

**Optionally**, add a cached `balance` column to `users` for performance:

```sql
ALTER TABLE users ADD COLUMN balance INTEGER NOT NULL DEFAULT 0;
```

If using a cached balance:
- It MUST be updated atomically within the same transaction as the ledger insert
- It MUST be reconcilable from the ledger at any time
- It is a **read optimization**, not the source of truth

### 6.3 Relationships

```
users (1) ──< transactions (N)
tasks (1) ──< transactions (N)  [via reference_type='task', reference_id=task_id]
users (1) ──< user_tasks (N) ──> tasks (N)
```

### 6.4 Why INTEGER for `amount`

The existing `tasks.reward` column is `INTEGER`. The transaction ledger should match:
- `amount INTEGER NOT NULL` — stored in the smallest unit
- Currency precision is an **open product decision** (see Section 12)
- If fractional rewards are needed, the unit can be defined (e.g., 1 = 0.01 currency units, or use TEXT with `Decimal`)

---

## 7. Precision Strategy

### 7.1 Current State

- `tasks.reward` is `INTEGER NOT NULL`
- No floating-point arithmetic exists anywhere in the codebase
- No currency or decimal precision is defined

### 7.2 Constraint: No Floating-Point for Financial Values

Python's `float` uses IEEE 754 binary representation, which cannot exactly represent values like `0.1`. For financial calculations:

| Approach | Pros | Cons |
|----------|------|------|
| `INTEGER` (smallest unit) | Fast, no precision loss, SQLite-native | Requires defining the unit |
| `TEXT` + Python `Decimal` | Maximum flexibility | Slower, requires careful handling |
| `REAL` | ❌ FORBIDDEN — binary floating-point | Precision loss |

### 7.3 Recommended Approach

**Use `INTEGER` in the smallest denomination**, similar to how currencies use cents/pence:

- Define a precision constant (e.g., `REWARD_PRECISION = 100` meaning 2 decimal places)
- Store `reward = 500` to mean "5.00 currency units"
- All arithmetic is integer-based
- Display layer divides by `REWARD_PRECISION` for formatting

### 7.4 Information Still Required

Before implementation, the following must be confirmed:

- **What is the currency unit?** (coins, stars, points, etc.)
- **What precision is needed?** (whole numbers only? 2 decimal places? more?)
- **Is there a conversion rate?** (e.g., 100 points = 1 coin)
- **Can rewards be fractional?** (the requirement says yes, but how fractional?)

---

## 8. Idempotency Strategy

### 8.1 Problem

If `CompletionGate.complete()` is called twice for the same (user_id, task_id), the second call raises `CompletionGateError` because `user_tasks.status` is already `completed`. This naturally prevents double-credit **if** the reward credit happens inside the completion transaction.

### 8.2 Idempotency Key

Each transaction row has a `UNIQUE(idempotency_key)`:

```python
idempotency_key = f"task_reward:{user_id}:{task_id}"
```

This guarantees:
- Even if the completion path is somehow replayed, the UNIQUE constraint prevents duplicate ledger entries
- `INSERT OR IGNORE` or `INSERT ... ON CONFLICT` can be used for silent idempotency
- The combination of (a) status transition guard + (b) idempotency key provides **defense in depth**

### 8.3 Atomic Operation

The entire credit operation MUST be a single SQLite transaction:

```python
with get_connection() as conn:
    # 1. Transition status
    conn.execute("UPDATE user_tasks SET status = 'completed', completed_at = CURRENT_TIMESTAMP ...")
    # 2. Insert ledger entry (idempotent)
    conn.execute("INSERT OR IGNORE INTO transactions (user_id, type, amount, ..., idempotency_key) ...")
    # 3. Optionally update cached balance
    conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
```

If any step fails, the entire transaction rolls back — no partial credit.

---

## 9. Completion / Reward Boundary

### 9.1 The Authoritative Boundary

```
                    ┌─────────────────────────────────┐
                    │     COMPLETION BOUNDARY          │
                    │  (single atomic transaction)     │
                    │                                   │
  Verification      │  1. user_tasks: started→completed │
  Result(PASSED) ──>│  2. transactions: INSERT credit   │
                    │  3. users: UPDATE balance (opt)   │
                    │                                   │
                    └─────────────────────────────────┘
```

### 9.2 Guarantees

| Guarantee | Mechanism |
|-----------|-----------|
| Completed task cannot double-credit | `user_tasks.status` already `completed` → `CompletionGateError` |
| Failed verification cannot credit | `CompletionGate` rejects non-PASSED `VerificationResult` |
| Incomplete task cannot credit | Credit only inside the completion transaction |
| Replaying completion cannot duplicate | `idempotency_key UNIQUE` constraint |
| Reward is tied to completion boundary | Credit is inside the same DB transaction as the status transition |
| Source of truth for balance is unambiguous | Balance = `SUM(transactions.amount)` for user |

### 9.3 Credit Timing

The reward is credited **at the moment of completion**, not at:
- Task start (would allow starting but never completing)
- Submission (verification hasn't passed yet)
- Some future batch job (introduces complexity and failure modes)

---

## 10. Home Integration

### 10.1 Current Home Structure

```
Home Page Sections:
  1. Welcome / Profile Summary  (connected to Telegram user data)
  2. Balance / Reward Summary   (placeholder: "—" for both)
  3. Daily Check-in             (placeholder: "قريباً")
  4. Official Guide             (placeholder: "قريباً")
  5. Add Task CTA               (disabled button)
  6. Account Linking            (placeholder: "قريباً")
  7. Hot Tasks                  (placeholder: "لا توجد مهام حالياً")
```

### 10.2 Future Home Balance Integration

Home should obtain balance from a **backend API endpoint** (not embedded in HTML):

```
GET /api/balance?user_id=12345
→ { "balance": 1500, "earned_total": 2000, "pending": 0 }
```

This endpoint should:
1. Verify Telegram Mini App auth (existing `miniapp_auth.py` validation)
2. Query the balance from the ledger: `SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = ?`
3. Return the balance and any summary statistics
4. Cache briefly (TTL 30-60s) to avoid hammering SQLite on every page load

### 10.3 Home Section Update

The `_buildBalanceSection()` in `home.js` currently renders:
```html
<span class="balance-value balance-empty">—</span>
```

Future state:
```html
<span class="balance-value" data-testid="balance-amount">1,500</span>
<span class="balance-value" data-testid="balance-available">1,500</span>
```

The "المكافآت" (Rewards/Earned Total) and "المتاح" (Available) distinction:
- **المكافآت**: Total lifetime earnings (sum of all credit transactions)
- **المتاح**: Available balance (earnings minus withdrawals)

### 10.4 Future Source

The Home page should read from a **balance/ledger service**, not directly from the database. The recommended data flow:

```
Mini App (home.js)
    ↓ fetch('/api/balance')
Flask endpoint (serve_miniapp.py or miniapp_auth.py)
    ↓
BalanceService.get_balance(user_id)
    ↓
db.get_balance(user_id)  -- reads from ledger or cached column
```

---

## 11. Future Compatibility

### 11.1 Extensibility of `transactions.type`

The `type` field in the transactions table supports future reward sources:

| Type | Description | Phase |
|------|-------------|-------|
| `task_reward` | Credit for completing a task | Next |
| `referral_reward` | Credit for successful referral | Future |
| `channel_reward` | Credit for channel subscription tasks | Future |
| `youtube_reward` | Credit for YouTube tasks | Future |
| `daily_checkin` | Credit for daily check-in | Future |
| `withdrawal` | Debit for withdrawal request | Future |
| `deposit` | Credit for top-up/deposit | Future |
| `admin_adjustment` | Manual admin credit/debit | Future |

### 11.2 Multiple Balance Types

If the product needs multiple balance types (e.g., "main balance" vs "bonus balance"):

**Option A**: Add a `balance_type` column to `transactions`:
```sql
transactions.balance_type TEXT NOT NULL DEFAULT 'main'
```

**Option B**: Separate wallet/balance tables keyed by type.

**Recommendation**: Option A is simpler and sufficient for early stages. The ledger architecture supports this without schema changes if the `type` field already distinguishes reward sources.

### 11.3 Withdrawals / Deposits

The header already has "الشحن" (Charge) and "السحب" (Withdraw) placeholder buttons. The ledger naturally supports:
- Withdrawals as negative-amount transactions with type `withdrawal`
- Deposits as positive-amount transactions with type `deposit`
- Balance validation: `SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type != 'withdrawal'` (available for withdrawal)

### 11.4 Referral Rewards

The `users.referred_by` field already captures referral attribution. A future `ReferralRewardService` would:
- Trigger on referred user's first task completion (or registration)
- Create a `referral_reward` transaction for the referrer
- Use idempotency key `f"referral_reward:{referrer_id}:{referred_id}"`

---

## 12. Open Product Decisions

> These are **NOT implemented** and require product confirmation before any code changes.

| # | Decision | Current State | Options |
|---|----------|---------------|---------|
| 1 | **Currency unit name** | Not defined | Coins, Stars, Points, TaskCoins, ... |
| 2 | **Reward precision** | `INTEGER` column | Whole numbers only? 2 decimals? Custom? |
| 3 | **Balance unit conversion** | Not defined | 1 reward = 1 balance unit? Conversion rate? |
| 4 | **Withdrawal rules** | Not defined | Minimum amount? Processing time? Methods? |
| 5 | **Deposit/top-up** | Not defined | How? Payment provider? In-app purchase? |
| 6 | **Referral reward amount** | Not defined | Fixed? Percentage of referee's reward? |
| 7 | **Channel/task rewards** | Not defined | Different reward tiers? Per-channel rewards? |
| 8 | **YouTube task rewards** | Not defined | How to verify? Reward amount? |
| 9 | **Daily check-in rewards** | Not defined | Streak bonuses? Fixed amount? |
| 10 | **Balance display precision** | Not defined | Show decimals? Abbreviate large numbers? |
| 11 | **Multiple balance types** | Not defined | Main + bonus? Or single balance? |
| 12 | **Negative balance** | Not defined | Allowed? Disallowed? Grace period? |

---

## 13. Explicit Non-Implementation List

The following are **NOT to be implemented** in this architecture micro-task:

- ❌ No database schema changes
- ❌ No new tables
- ❌ No new API endpoints
- ❌ No changes to the task lifecycle pipeline
- ❌ No changes to `CompletionGate`, `TaskStartGate`, or any existing boundary
- ❌ No changes to the Mini App UI
- ❌ No changes to `home.js`, `header.js`, `app.js`, or CSS
- ❌ No balance crediting logic
- ❌ No transaction/ledger implementation
- ❌ No withdrawal or deposit flows
- ❌ No referral reward implementation
- ❌ No YouTube reward implementation
- ❌ No channel reward implementation
- ❌ No daily check-in implementation
- ❌ No currency or precision configuration
- ❌ No floating-point arithmetic for financial values
- ❌ No migration scripts
- ❌ No `INTEGER` → `REAL` type changes
- ❌ No new Python packages
- ❌ No changes to `requirements.txt`

---

## Appendix A: File Inventory

| File | Role in Balance System |
|------|----------------------|
| `db.py` | Will need new tables + balance queries |
| `task_completion.py` | CompletionGate — credit hook point |
| `completion_bridge.py` | Orchestrator — may invoke RewardService |
| `task_lifecycle.py` | Top-level orchestrator — may wire reward callback |
| `task_start.py` | **No changes** |
| `task_attempt.py` | **No changes** |
| `task_submission.py` | **No changes** |
| `task_verifier.py` | **No changes** |
| `task_catalog.py` | May expose reward info for catalog display |
| `miniapp/js/home.js` | Will need to fetch and display balance |
| `miniapp/js/header.js` | Will need to wire charge/withdraw buttons |
| `serve_miniapp.py` | Will need balance API endpoint |
| `miniapp_auth.py` | Auth validation reused for balance API |
| `config.py` | May need currency/precision constants |
| `subscription.py` | **No changes** |

## Appendix B: Test Guardrails to Preserve

These existing tests assert that reward/balance logic does NOT leak into wrong modules:

1. `test_task_completion.py::test_no_reward_granted` — no reward records after completion
2. `test_task_lifecycle.py::test_no_reward_logic` — no reward keyword in TaskLifecycle source
3. `test_task_submission.py::test_reward_field_rejected` — client cannot inject reward
4. `test_subscription.py::test_no_reward_deduction_code` — subscription has no reward logic
5. `test_miniapp_home.py::test_no_hardcoded_balance_numbers` — no fake amounts in UI
6. `test_miniapp_home.py::test_no_fake_reward_values` — no invented reward values

When the balance system is implemented, these tests should be:
- **Updated** (not deleted) to reflect the new reality where crediting exists in the correct module
- **NOT broken** by adding reward logic to the wrong module

---

*This document is an architecture analysis only. No production code has been modified.*
