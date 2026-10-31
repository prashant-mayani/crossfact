# Measuring Crossfact

Does making two models from different families argue out their conflicts give
better answers than asking one model? Or than cheaper ways of using several
models? This page is how that gets measured, what the numbers can and can't
say, and (once there are numbers) what they showed, wins or not.

## Status

The harness (`bench/`) is built and tested end to end with scripted models:
every system, the real Crossfact pipeline, grading, resuming, budgets, the
statistics. **No real results yet.** The first published run is the free
comparison below, about four days of free-tier quota; the table at the end is
empty until then.

## Phase 0: what is measured

**Questions.** [SimpleQA Verified](https://arxiv.org/html/2509.07968v2) (Haas
et al., Google DeepMind and Google Research, 2025): 1,000 short factual
questions, each with one verified answer, to be answered from a model's own
knowledge. It is a cleaned-up, rebalanced, harder subset of OpenAI's SimpleQA
(4,326 questions filtered down for duplicates, conflicting sources, topic
balance and headroom). The free run uses a topic-stratified sample of 200
(seed 1). The first download pins the file's sha256 in
`bench/datasets.lock.json`; a later download that differs is refused, so a
silently changed dataset can't change the numbers.

**Closed-book, one instruction.** No system gets search or tools. Every system
gets the same question and the same instruction: *reply with only the answer;
if you are not confident it is correct, reply "I don't know"*. Crossfact's
pipeline sees it too, after the question (mode `auto` picks its route from the
question alone).

**The systems** (`bench/configs/free.yaml`, free models only, so anyone can
rerun it at $0):

| system | what it does | new calls per question (about) |
|---|---|---|
| `single-gpt-oss`, `single-gemini`, `single-qwen` | one model, one call: GPT-OSS 120B (Groq), Gemini 3.5 Flash-Lite, Qwen 3.8 27B (Groq) | 1 each |
| `selfcheck-gpt-oss`, `selfcheck-gemini` | the same model checks its own answer and gives a final one | 1 |
| `selfconsistency-gpt-oss` | GPT-OSS asked three times; the majority answer, or "I don't know" | 2 |
| `vote-3` | the three models; an answer at least two give, or "I don't know" | 0 |
| `moa-3` | Mixture-of-Agents, one layer: GPT-OSS reads all three answers and writes the final one | 1 |
| `crossfact` | the app's pipeline in consensus mode, unchanged: GPT-OSS and Gemini answer, Flash-Lite compares and merges, they debate only conflicts, Qwen judges what is left | 2 (they agree) to 12 (three rounds and a ruling) |
| `crossfact-auto` | the app's default, mode `auto` (docs/DESIGN.md, "Routes"): GPT-OSS and Gemini answer; a short fact is answered only when both give it, or when Qwen gives it on its own beside a "not sure"; otherwise "not sure". Other questions run the consensus above, and calculations are computed in code | 0 to 4 for a short fact; as `crossfact` otherwise |

Why these baselines:

- **Self-check** costs what a second opinion costs, from the same model. If
  Crossfact beats single models but not self-check, the gain was the extra look,
  not the second model. (Without outside feedback, self-correction lowered
  accuracy on reasoning benchmarks in [Huang et al., ICLR 2024](https://arxiv.org/pdf/2310.01798).)
- **Self-consistency** is a baseline that multi-agent debate methods often fail
  to beat ([Zhang et al., 2025](https://arxiv.org/html/2502.08788v3), across 5
  debate methods and 9 benchmarks; the same paper finds mixing model families
  helps debate, which is Crossfact's design). A 2026 study of identical small
  models found a model correcting itself matched or beat unguided debate at a
  half to a third of the tokens ([The Cost of Consensus](https://arxiv.org/pdf/2605.00914)).
- **Vote** and **Mixture-of-Agents** ([Wang et al., 2024](https://arxiv.org/pdf/2406.04692))
  use the same three models without debate: is arguing better than counting,
  or than one model reading the others?
- Debate can also talk a model out of a right answer ([Wynn et al., Talk Isn't
  Always Cheap](https://arxiv.org/pdf/2509.05396v2)). The "lost" column below
  counts exactly that.

**Grading.** Every answer is graded correct, incorrect or not attempted with
OpenAI's SimpleQA grader prompt (simple-evals, pinned to commit `652c89d`, file
checked by sha256 and read as data, never run), plus SimpleQA Verified's
changes to it, in our own words: a numeric gold target states its acceptable
range; only the direct answer is graded; an answer that lists candidates must
commit to one or it is *not attempted*; declining in any phrasing is *not
attempted*. Exact matches and bare "I don't know"s skip the grader. The prompt
asks for a bare letter, and a reply that is just a letter or just a grade word
("Correct.") counts. A reply that explains instead is read only by the grade
words it writes in the prompt's capitals (CORRECT, INCORRECT, NOT_ATTEMPTED):
never by a leading word "A" ("A predicted answer of 1923 contradicts the gold
target, so it is INCORRECT" is INCORRECT), and never by lower-case prose ("does
not match the correct answer" says nothing about the grade). A grade word after
a negation, a letter and words that disagree, or two different grades, and the
grader is asked again. The parser's version is part of the grader's
identity, so a fix to it re-grades everything from the stored replies, at no
cost, instead of keeping grades the old parser got wrong.

The grader model is **Llama 3.3 70B on Workers AI**, a fourth family that
answers nothing here. SimpleQA Verified's own autorater is a paid model
(gpt-4.1-2025-04-14), so **these are not leaderboard numbers**: they compare
the systems with each other, all graded the same way.

**What is reported**, per system, always side by side:

- **correct, wrong and not sure** as shares of the graded questions, with 95%
  bootstrap intervals over questions. A system that abstains more looks more
  accurate when it answers; showing all three keeps that visible.
- **accuracy when it answered** and **F1** (SimpleQA's headline: the harmonic
  mean of the correct rate and accuracy-when-answered).
- **errors**: questions lost to provider trouble, counted and not scored.
- **paired comparison** with the reference (the best single model by F1, picked
  after the fact, which flatters the reference: conservative for the
  multi-model systems): the change in correct and in wrong answers on the
  questions both answered, each with an exact McNemar test, Holm-adjusted
  across the table.
- **lost**: a participant (a model the system asked, or self-check's first
  answer) was right and the final answer wasn't. **Found**: the final answer
  was right and no participant was.
- **when some participants abstained**: on the questions where one model said
  "I don't know" and another answered, how often the system's final answer was
  right, wrong, or an abstention. It needs no extra calls, and it explains most
  of Crossfact's numbers on this test (next section).

## Decided before the run

Written down before any real result, so the numbers can't choose the question.

**The headline** is one comparison: **Crossfact as the app runs it
(`crossfact-auto`) against the best single model by F1**, on the questions both
answered, on two measures: the change in **wrong answers** (exact McNemar test)
and the change in **F1** (paired permutation test, with a paired bootstrap
interval), Holm-adjusted over the two. Wrong answers alone would reward a
system for abstaining; F1 falls when abstaining costs more right answers than
it saves wrong ones, so the two together say whether "not sure" was worth it.
It is printed first and named as the headline in every report (`primary` in the
config). Everything else is secondary: the comparison table holds eighteen
p-values, and among that many about one comes in under 0.05 by chance alone, so
the seventeen besides the headline's are Holm-adjusted across the table and read
as leads, not findings. (The headline moved from consensus-mode `crossfact` to
`crossfact-auto` when routing became the default, before any run.)

**What we expect: Crossfact v2 will not reduce wrong answers much here.** When
one model says "I don't know" and the other guesses, the comparator records the
guess as a point only one side made, and the merge must keep every point either
side made, so Crossfact commits to the guess. It holds back only when both models
answer, differently, and the judge can't decide between them. So on this test
Crossfact should look like its more willing model, plus whatever debate settles
on the two-way disagreements. That would be a fair baseline result, not a bug,
and the case for v3's separate route for short factual questions. The "some
participants abstained" table shows it directly.

**What we expect from `crossfact-auto`: fewer wrong answers, and fewer right
ones.** It answers a short fact only when two models give the same answer on
their own, so a guess one model makes and the other doesn't share becomes "not
sure". Whether that beats the best single model on F1 depends on how often the
two models are right together; the "lost" column counts the right answers it
gave up. Exact questions don't occur in SimpleQA; on them the route is right by
construction (`bench/sets/exact.yaml`, 13 of 30 taken, all right, the rest left
to the models; a test keeps it that way).

## Keeping it fair

- **Same models.** Crossfact's two sides are two of the single baselines; its
  judge, which is also `crossfact-auto`'s third model, is the third family. No
  fallback models: a fallback would make the comparison unequal.
- **Shared answers.** The same model asked the same prompt is one call, cached:
  Crossfact's first answers *are* the single baselines' answers, so the paired
  comparison isolates what the pipeline adds.
- **Failures aren't scored, for anyone.** A failed call (timeout, server error)
  records the answer as an error; a later run tries again (three times, then it
  stays an error and is counted). For Crossfact, a run in which any provider call
  failed is treated the same way, because a missing side or a cut-short debate
  is the network's doing, not the method's. What the method gets wrong itself
  (a judge reply that isn't JSON) is scored. Daily quotas and per-minute limits
  are pacing: the run waits or pauses, it never scores them.
- **Game days don't leak in.** Faults injected from the admin page are ignored
  by the harness.
- **One version per run.** A run records the code that decides answers and
  grades, the effective model registry, the dataset and the grader prompt, all
  by hash. Resuming a run on different code or a different registry is refused
  (`--allow-version-change` overrides it, and the report then says the run
  mixed versions). The dataset and grader prompt can't change mid-run at all.
- **Closed-book, Crossfact included.** The app's short-fact route can search
  the web (docs/DESIGN.md, "Search"); the harness turns that off unless a
  config has a `search:` section. An open-book comparison needs baselines with
  the same search (one model reading the same pages), and must keep out pages
  that copy the benchmark's answers (`search.exclude_domains`); until then a
  win with search would show that search helps, which is known.
- **Crossfact's time limits are relaxed** in the free run (30 minutes instead of
  10, and 15 minutes instead of 150 s for a side that lags the other): waiting
  out free-tier rate limits isn't slowness. Nothing else in the pipeline
  changes.

## What the numbers can't tell you

- **The grader is a model.** Its mistakes hit every system, but not necessarily
  evenly: a long merged answer may be graded differently from a terse one. The
  instruction asks everyone for the bare answer, and the grading rules judge
  only the direct answer. `python -m bench grade-check --run <id> --grader
  <provider/model>` re-grades a sample with a second grader and reports
  agreement and Cohen's kappa.
- **Short answers only.** SimpleQA tests recall of single facts. Crossfact's merge
  is written for long answers: it keeps every point from both sides (so it
  commits to a guess when the other side abstains), and when the judge can't
  settle a conflict the answer presents both positions, which SimpleQA Verified
  grades as not attempted. How Crossfact does on long, multi-part answers is a
  separate question this phase doesn't answer.
- **Literal voting.** Vote counts answers as the same only when they match
  after normalizing (case, punctuation, a leading article, the value of a
  number). "Shakespeare" and "William Shakespeare" are two answers, which
  undercounts agreement and works against vote.
- **One draw per question.** Models sample; the cache makes a rerun reproduce
  the same draw rather than average over draws. The intervals cover the choice
  of questions, not the models' randomness.
- **200 questions** give intervals of roughly ±5 to ±7 points on each rate.
  Paired tests detect smaller differences than overlapping intervals suggest,
  but a difference of a few points is not a finding.
- **Free models, not the app's defaults.** Phase 0 compares GPT-OSS, Gemini
  Flash-Lite and Qwen, because a subscription has no API credits and the
  published run must be reproducible at $0. The app's default pairing (Claude
  with Gemini 3.8 Flash) gets only the nightly check below.
- **Latency** in the free run means little (shared answers are cached, and
  rate-limit waits are subtracted). The nightly measures it without a cache.

## Running it

### Before you start: the free run and your app share these allowances

Groq's limits apply to the whole organization (a second key shares them) and
Workers AI's to the whole account. With its default budgets, the free run uses
nearly all of GPT-OSS's daily tokens on Groq and all of Workers AI's daily
neurons, which are where your deployed app's judge runs: for the four days of
the run, the app's judge would fall back to Gemini and then Claude. So:

- **start it before you deploy**, while nothing else uses the allowances, and
- **once the app is live on the same accounts, add `--leave-for-app 0.5`** to
  the daily command: every budget is halved, the app keeps about half of each
  day's allowance, and the remaining days take about twice as long. Budgets
  can change mid-run; the run carries on where it was.

Don't use a second Groq account for it: Groq's usage policy forbids registering
multiple accounts, or coordinating usage across organizations, to go beyond its
limits ([Groq AI policy](https://console.groq.com/docs/legal/ai-policy)); it
says research needs can ask Groq for an exception.

### The free comparison, on your laptop

Keys stay in your shell, on your machine; the config file holds only the
Cloudflare account ID, which isn't a secret. Prompts go to Groq, Google and
Cloudflare (the questions are a public benchmark; Gemini's free tier may use
prompts to improve Google's products).

```bash
python3.12 -m venv .venv && . .venv/bin/activate              # the pins are for Python 3.12
pip install --require-hashes --no-deps -r requirements.txt
export CROSSFACT_SECRETS_MODE=env
read -rs GROQ_API_KEY && export GROQ_API_KEY            # console.groq.com
read -rs GEMINI_API_KEY && export GEMINI_API_KEY        # aistudio.google.com
read -rs CLOUDFLARE_AI_TOKEN && export CLOUDFLARE_AI_TOKEN   # a token with Workers AI permission
mkdir -p bench/runs
echo "provider_vars: {cloudflare_account_id: YOUR_ACCOUNT_ID}" > bench/runs/config.yaml
export CROSSFACT_CONFIG=$PWD/bench/runs/config.yaml

python -m bench run --config bench/configs/free.yaml --limit 50   # a first look: about a day of quota
python -m bench run --config bench/configs/free.yaml              # the 200: run it again each day until done
#   add --leave-for-app 0.5 to both if your app is deployed on the same accounts
python -m bench report --run <run id> --md docs/eval/free.md      # `python -m bench runs` lists run ids
git add bench/datasets.lock.json                                   # pin the dataset you measured
```

A run checks first that every model it needs has a key, and stops before
spending anything if not. It stops when a daily budget is reached (exit status
3) and the same command continues it; the 50-question sample is (almost
always) part of the 200, so the first look is reused. Everything lives in `bench/runs/bench.db`
(git-ignored): every model call and grade, so a report or a rerun costs no
calls.

### The nightly check, in your cluster

The chart's `eval.nightly` CronJob runs `bench/configs/nightly.yaml` every night
at 00:37 Pacific (after the Gemini free tier's daily reset): the same ten
questions through Claude alone, Claude checking itself, and Crossfact with Claude
and Gemini Flash-Lite, uncached, graded the same way. Claude's calls count
against your plan like any message, 20 to 40 a night, so it is off by default.
Turn it on in the deploy repo's Argo CD Application for Crossfact:

```yaml
helm:
  valuesObject:
    eval:
      nightly:
        enabled: true
```

It needs the same keys the app already has (Groq, Gemini, Workers AI). Results
go to `/data/bench` on the volume and to the `crossfact_eval_*` metrics, shown on
the overview dashboard's last row; `CrossfactEvalStale` says when it stops
finishing. Ten questions are for watching drift (the same questions every
night, so a change means a model or provider changed), not for comparing
systems: one question is ten points.

## Throughput on free tiers

Free tiers cap requests and tokens per day, so the free run takes several days.
Limits as published at the time of writing; check your own.

| provider | free limit | used per question (estimate) | questions a day |
|---|---|---|---|
| Groq: GPT-OSS 120B, Qwen 3.8 27B | 30 requests and 8K tokens a minute, 1K requests and 200K tokens a day, per model ([Groq](https://console.groq.com/docs/rate-limits)) | GPT-OSS: about 6 calls, about 3,600 tokens | about 55 |
| Gemini 3.5 Flash-Lite | per project, shown in AI Studio; resets at midnight Pacific ([Google](https://ai.google.dev/gemini-api/docs/rate-limits?authuser=1)). Users report about 500 a day ([forum](https://discuss.ai.google.dev/t/gemini-3-8-flash-free-tier-20-rpd-is-too-limited-for-practical-evaluation/180609)) | about 5 calls | about 90 (our budget: 450) |
| Workers AI: the grader | 10,000 neurons a day, reset 00:00 UTC; Llama 3.3 70B costs 26,668 neurons per million input tokens ([Cloudflare](https://developers.cloudflare.com/workers-ai/platform/pricing/)) | about 4 model grades of about 47 neurons | about 50 |

So about 50 questions a day: the 200 in about four days (eight with
`--leave-for-app 0.5`), the full 1,000 in about three weeks. The grader is the
bottleneck; answers that match the gold target exactly, and bare "I don't
know"s, skip it. Groq is budgeted in tokens too, from the counts it reports, so
the run stops just under its daily token limit instead of running into it. The
run prints its calls as it goes, and `budgets`, `token_budgets` and `pace` in
the config can change mid-run.

## Results

None yet. Each run's tables come from `python -m bench report`; they will be
added here as they are, with the run id, the code version and the dataset hash.
