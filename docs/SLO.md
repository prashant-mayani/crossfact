# SLOs, alerts and runbooks

Three promises, measured over a rolling 28 days, with alerts that fire when the
error budget is burning fast enough to matter and stay quiet otherwise. The
rules live in `charts/crossfact/templates/prometheusrule.yaml`;
`charts/crossfact/tests/slo-rules.test.yaml` proves them with promtool in CI.

| SLO | SLI (good events / valid events) | objective | budget over 28 days |
|---|---|---|---|
| [Availability](#availability) | synthetic probes of `https://<app>/healthz` through Cloudflare that succeed / probes expected | 99.5% | about 3 h 22 min of failed probes |
| [Consensus success](#consensus-success) | consensus runs ending `agreed`, `debated` or `judged` / all consensus runs | 95% | 1 run in 20 may end degraded or failed |
| [Consensus latency](#consensus-latency) | successful consensus runs finishing within 240 s / successful consensus runs | 95% | 1 successful run in 20 may take longer |

Why these three. Availability is the one users notice first: can I reach it at
all. Consensus success is the product's real promise: a question asked for a
consensus gets one, not a single model's answer in disguise. Latency matters
because a run is a wait, and 240 s is roughly where waiting turns into giving
up; it is a histogram bucket boundary, so the SLI is exact.

## Availability

**Measured from outside the app, through the whole path.** The blackbox
exporter in the cluster requests the public URL every 30 s, so each probe
crosses DNS, Cloudflare's edge, the tunnel, cloudflared, the Service and the
app. A probe failing anywhere on that path counts.

**Gaps count as downtime.** If the node is down, the prober is down too and
there are simply no samples; an average over the samples that exist would read
100%. So the 28-day SLI counts *expected* probes (elapsed time / 30 s) rather
than received ones, starting from the first probe ever (a new install isn't
judged on days before it existed):

```promql
clamp_max(
  sum_over_time(probe_success{job="crossfact-public"}[28d])
  / clamp_min((time() - min_over_time(timestamp(probe_success{job="crossfact-public"})[28d:1h])) / 30, 1),
  1)
```

**And when everything is down, nothing in the cluster can tell you.** That is
what the heartbeat is for: Alertmanager pings healthchecks.io every minute
(Prometheus's always-firing `Watchdog` alert routed to a webhook). When the
pings stop, healthchecks.io emails you from outside.

**Alerts** (multiwindow, multi-burn-rate, from the Google SRE workbook):

| alert | condition | severity | means |
|---|---|---|---|
| `CrossfactAvailabilityFastBurn` | error ratio > 14.4 × 0.5% (7.2%) over **both** 1 h and 5 m, for 2 m | critical (Telegram) | at this rate the month's budget is gone in about 2 days; a full outage fires in about 6 to 8 minutes |
| `CrossfactAvailabilitySlowBurn` | > 6 × 0.5% (3%) over both 6 h and 30 m, for 15 m | warning | a steady leak: about 5 days to exhaustion |

The long window gives significance; the short one makes the alert stop soon
after recovery. One failed probe in an hour (0.8%) fires nothing; promtool
proves both.

**When it fires:**
1. Is it reachable at all? `curl -sS -o /dev/null -w '%{http_code}\n' https://<app>/healthz`.
   A Cloudflare error page (1033 = tunnel down) points at the tunnel; a 502/503
   from the app at the pod; a timeout at the node or Cloudflare.
2. The tunnel: `kubectl -n cloudflared get pods`, `kubectl -n cloudflared logs deploy/cloudflared`.
   `AppTunnelDown` fires alongside if no connector is connected.
3. The app: `kubectl -n crossfact get pods`, events, `kubectl -n crossfact logs deploy/crossfact`.
4. The node: if kubectl can't connect, use ssh; if neither, the OCI Console
   (instance state, serial console) and the break-glass steps in the deploy
   repo's OPERATIONS.md.
5. Not us: Cloudflare status page; a challenge from Cloudflare security
   settings (Bot Fight Mode must stay off: it can challenge the prober).

## Consensus success

```promql
1 - sum(increase(crossfact_runs_total{mode="consensus",outcome=~"agreed|debated|judged"}[W]))
  / sum(increase(crossfact_runs_total{mode="consensus"}[W]))
```

**The low-traffic guard.** A personal app might see a handful of runs a day.
With two runs in six hours, one bad run is a 50% error rate, which would fire a
"14.4× burn" alert for a single unlucky question. So the run-based alerts also
require a minimum number of runs in the window (`slo.minRunsForBurnAlert`, 6;
24 for the three-day alert). Below that, the dashboard still shows the ratio,
and the 28-day SLI still counts every run.

| alert | condition | severity |
|---|---|---|
| `CrossfactConsensusSuccessBurn` | error ratio > 6 × 5% (30%) over 6 h and 1 h, with ≥ 6 runs in 6 h | warning |
| `CrossfactConsensusSuccessSlowBurn` | > 5% over 3 d and 6 h, with ≥ 24 runs in 3 d | warning |

These warn rather than page: the usual cause is a free tier running out or a
provider having a bad day, which a person at 3 a.m. can't fix, and every
affected answer already tells the user what happened.

**When it fires:**
1. Grafana, "Crossfact overview": *Runs by outcome* shows which degraded reason
   dominates; *Call outcomes* and *Retries by cause* show which provider.
2. `side_*_failed` with `quota_exhausted`: a free daily quota ran out
   (`CrossfactQuotaExhausted`). Fallbacks should be carrying it; if not, add a
   fallback model in `providers.yaml`, or wait for the daily reset.
3. `auth`: see [Auth](#auth). `circuit_open`: see [Circuit open](#circuit-open).
4. `deadline`: a slow provider; see [Consensus latency](#consensus-latency).
5. `comparator_failed`/`merge_failed` with `bad_json`: the utility model's
   output changed shape. Try the next utility fallback first in the registry.

## Consensus latency

```promql
1 - sum(increase(crossfact_run_duration_seconds_bucket{mode="consensus",result="success",le="240.0"}[W]))
  / sum(increase(crossfact_run_duration_seconds_count{mode="consensus",result="success"}[W]))
```

Only successful runs count, so a provider failing fast can't make latency look
good. Alerts mirror consensus success (`CrossfactConsensusLatencyBurn`,
`CrossfactConsensusLatencySlowBurn`, same guards).

**When it fires:** open a slow run's trace from an exemplar dot on the
*Run duration* panel (or Tempo search: `{ name = "consensus.run" && duration > 240s }`).
The `provider.call` spans show which model took the time and whether retries
(span events) did. Then *p95 model-call latency by model* shows whether it is
one model or all. Typical causes: a provider slow today (switch the default
side or effort), debates hitting three rounds (look at `debate` spans), or
429s with retry hints adding 20 s each (retries by cause = `rate_limited`).

## Operational alerts

<a id="crossfactdown"></a>
### CrossfactDown (critical)

No Crossfact pod has been scraped successfully for 5 minutes.
`kubectl -n crossfact get pods -o wide`, `kubectl -n crossfact describe pod ...`.
Common causes: image pull refused by the signature policy (events say
"no matching signatures"), an unset secret (ExternalSecret not Ready), the
volume (`kubectl -n crossfact get pvc`), or the node itself.

<a id="circuit-open"></a>
### CrossfactCircuitOpen (warning)

A provider/model has failed repeatedly and calls to it are paused (the breaker
retries one call after the cooldown, doubling it up to 10-15 minutes while
failures continue). Runs keep working with substitutes or degrade. Look at
*Call outcomes* for that model to see why (timeout, server, auth, quota). It
closes by itself on the first successful trial call.

<a id="auth"></a>
### CrossfactProviderAuthFailing (warning)

A provider rejected our credentials. Most likely the Claude token (it lasts
about a year: run `claude setup-token` and store it with the deploy repo's
`scripts/set-secret.sh claude_oauth_token`) or a revoked Gemini key. Secrets are
re-read every 60 s after External Secrets syncs them (hourly, or force it:
`kubectl -n crossfact annotate externalsecret crossfact-secrets force-sync=$(date +%s) --overwrite`).

<a id="quota"></a>
### CrossfactQuotaExhausted (info)

A free daily quota ran out for a model; it resets daily (Gemini: midnight
Pacific). Fallbacks carry the load. Informational unless consensus success
burns too.

<a id="abandoned"></a>
### CrossfactRunsAbandoned (warning)

A pod died with runs in flight (OOM kill, crash, forced delete, node failure).
Those runs were closed out, refunded, and offered for Retry. Find out why the
pod died: `kubectl -n crossfact get pods` (restarts, `OOMKilled` in
`describe`), node events. Planned deploys and reboots never cause this; they
drain.

<a id="backups"></a>
### CrossfactBackupStale / CrossfactBackupVerifyStale (warning)

No successful nightly backup in 26 hours, or no successful weekly restore test
in 8 days. `kubectl -n crossfact get jobs`, then the logs of the newest failed
one. The verify job also fails when the bucket outgrows its 15 GB budget (the
free allowance is 20 GB): shorten backup or upload retention. Run either by
hand: `kubectl -n crossfact create job --from=cronjob/crossfact-backup backup-now`.

If the log says `backup REFUSED`, the backup in the bucket holds conversations
and the live database holds none (or under a tenth as many): it looks like the
app started on an empty database, and uploading would replace the real backup.
Nothing was uploaded. Find out why the database is empty before anything else
(was the volume replaced? did the restore step run? `kubectl -n crossfact logs
deploy/crossfact -c restore`). To get the data back, restore it (the deploy repo's
`scripts/restore.sh latest`). Only if you deleted everything on purpose, run the
backup once with `--force`:

    kubectl -n crossfact create job --from=cronjob/crossfact-backup backup-force --dry-run=client -o json \
      | jq '.spec.template.spec.containers[0].command += ["--force"]' | kubectl apply -f -

<a id="restore-blocked"></a>
### CrossfactRestoreBlocked (warning)

The app pod's first step restores the latest backup when the volume has no
database (after a rebuild). It found a backup it could not read, so it is
holding the app down rather than starting it on an empty database. Read why:
`kubectl -n crossfact logs deploy/crossfact -c restore`. The step retries on its
own; once the cause is fixed the app starts with its data. Causes, by the error
it prints:

- `NotAuthenticated` (401): the backup user's key, fingerprint or user OCID is
  wrong, or the key was rotated. Compare the chart values `backup.auth.*` with
  `terraform -chdir=terraform/foundation output` and the Vault secret
  `backup_api_private_key`.
- `NotAuthorizedOrNotFound` / `BucketNotFound` (404): the policy doesn't cover
  the bucket, or `backup.bucket`/`backup.namespace` is wrong.
- `backup key ... not found`: the `crossfact-backup-key` ExternalSecret hasn't
  synced (`kubectl -n crossfact get externalsecret`).
- `damaged`: the latest backup doesn't open. Put an older one back with the
  deploy repo's `scripts/restore.sh <object>`.

To start with an empty database on purpose (the data really is gone), set the
chart value `restore.enabled: false` for one sync, then set it back, and run
the next backup with `--force` (above): the backup check would otherwise refuse
to replace the old backup with the empty database.

## How the rules are tested

`charts/crossfact/tests/render.sh` renders the chart, extracts the rules, runs
`promtool check rules`, then `promtool test rules`:

- a 90-minute outage pages, with the expected labels and annotations
- one failed probe in an hour doesn't
- half of consensus runs degraded over 6 h warns
- two bad runs out of two don't (the low-traffic guard)
- half of successful runs slower than 240 s warns
- no scrapeable pod fires `CrossfactDown`; an abandoned run fires `CrossfactRunsAbandoned`
- a restore step stuck in CrashLoopBackOff for 5 minutes fires `CrossfactRestoreBlocked`
- the availability SLI counts a gap in the data as downtime

## Dashboards

*Crossfact SLOs*: each SLI over 28 days, budget remaining, error ratio per window
with run counts. *Crossfact overview*: runs and outcomes, run and model-call
latency with exemplars, call outcomes, retries by cause, breaker states,
substitutions, HTTP, drain/orphans/faults.
