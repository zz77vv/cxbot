# JR account cooldown

Approved scope: the user requested implementation and deployment of independent
60-second free-account cooldowns, selecting another eligible account while one
is cooling, and waiting for the earliest available account if all are cooling.
Members are exempt from the proactive delay; explicit JR rate-limit replies
override cached membership. Telegram FloodWait and ambiguous paid operations
retain their existing handling.

Approach: persist JR cooldowns separately from Telegram cooldowns in schema v2;
check them during ranking and atomically during task reservation. An account
lease rechecks candidates after contention and sleeps without holding account
locks. A recognized rejection before purchase releases the reservation and
returns the same task to scheduling. Cache hits never reserve an account.

- [ ] Add regression tests for parsing, atomic cooldown, migration/restart,
  account rotation, member exemption, queue wakeup and explicit rejections.
- [ ] Run those tests against the old code and observe failures.
- [ ] Implement schema v2, task reservation, selector and scheduler integration.
- [ ] Run targeted and complete offline tests, compile checks and dependency check.
- [ ] Review the diff, prepare a deployable patch and inspect the actual server.
- [ ] Back up, stop the existing service, deploy, restart and verify health;
  roll back code on failure and report any unresolved production checks.

Defaults: unknown membership uses the free-account delay. The upstream numeric
delay is honored and never shortens an existing cooldown. A network timeout,
purchase or pagination failure never rotates and resubmits a query.
