// Load test: p50 / p99 per endpoint.
//   docker compose --profile loadtest run --rm loadtest
//   VUS=100 DURATION=60s LABEL=10M docker compose --profile loadtest run --rm loadtest
//
// Phase 1 "mixed":     VUS clients over random seeded users
//                      (50% balance, 30% credit, 20% debit).
// Phase 2 "hot":       half the clients hammer ONE user (u000001),
//                      the other half keep doing mixed traffic on other
//                      users ("hot_others") to show isolation.
// 404 and 409 are expected outcomes, not failures.
import http from 'k6/http';
import { check } from 'k6';

const BASE = __ENV.BASE_URL || 'http://localhost:8080';
const TOKEN = __ENV.API_TOKEN || 'recruitment-test-token';
const VUS = parseInt(__ENV.VUS || '100', 10);
const DURATION = __ENV.DURATION || '60s';
const SEED_USERS = parseInt(__ENV.SEED_USERS || '100000', 10);
const LABEL = __ENV.LABEL || 'run';
const HOT_USER = 'u000001';

const PARAMS = { headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' } };
http.setResponseCallback(http.expectedStatuses(200, 404, 409));

const durationSeconds = parseInt(DURATION, 10) * (DURATION.endsWith('m') ? 60 : 1);
const SCENARIOS = ['mixed', 'hot_user', 'hot_others'];
const ENDPOINTS = ['credit', 'debit', 'balance'];

const thresholds = { checks: ['rate>0.999'] };
for (const s of SCENARIOS) {
  for (const e of ENDPOINTS) {
    // Thresholds make k6 report a sub-metric per (scenario, endpoint).
    thresholds[`http_req_duration{scenario:${s},endpoint:${e}}`] = ['max>=0'];
  }
}

export const options = {
  discardResponseBodies: false,
  summaryTrendStats: ['avg', 'p(50)', 'p(90)', 'p(99)', 'max', 'count'],
  thresholds,
  scenarios: {
    mixed: { executor: 'constant-vus', exec: 'mixed', vus: VUS, duration: DURATION },
    hot_user: {
      executor: 'constant-vus', exec: 'hot', vus: Math.ceil(VUS / 2), duration: DURATION,
      startTime: `${durationSeconds + 5}s`,
    },
    hot_others: {
      executor: 'constant-vus', exec: 'mixed', vus: Math.floor(VUS / 2), duration: DURATION,
      startTime: `${durationSeconds + 5}s`,
    },
  },
};

function randomUser() {
  const i = 1 + Math.floor(Math.random() * SEED_USERS);
  return 'u' + String(i).padStart(6, '0');
}

function randomAmount(max) {
  return 1 + Math.floor(Math.random() * max);
}

function doCredit(user) {
  const r = http.post(`${BASE}/credit`, JSON.stringify({ user_id: user, amount: randomAmount(10000) }),
    Object.assign({}, PARAMS, { tags: { endpoint: 'credit' } }));
  check(r, { 'credit 200': (x) => x.status === 200 });
}

function doDebit(user) {
  const r = http.post(`${BASE}/debit`, JSON.stringify({ user_id: user, amount: randomAmount(5000) }),
    Object.assign({}, PARAMS, { tags: { endpoint: 'debit' } }));
  check(r, { 'debit 200/409': (x) => x.status === 200 || x.status === 409 });
}

function doBalance(user) {
  const r = http.get(`${BASE}/balance/${user}`, Object.assign({}, PARAMS, { tags: { endpoint: 'balance' } }));
  check(r, { 'balance 200': (x) => x.status === 200 });
}

function step(user) {
  const p = Math.random();
  if (p < 0.5) doBalance(user);
  else if (p < 0.8) doCredit(user);
  else doDebit(user);
}

export function mixed() { step(randomUser()); }
export function hot() { step(HOT_USER); }

function fmt(ms) { return ms === undefined ? '-' : ms.toFixed(2); }

export function handleSummary(data) {
  const lines = [`Results (${LABEL}), latency in ms`, '',
    '| scenario | endpoint | requests | p50 | p90 | p99 | max |',
    '|---|---|---|---|---|---|---|'];
  for (const s of SCENARIOS) {
    for (const e of ENDPOINTS) {
      const m = data.metrics[`http_req_duration{scenario:${s},endpoint:${e}}`];
      if (!m) continue;
      const v = m.values;
      lines.push(`| ${s} | ${e} | ${v.count} | ${fmt(v['p(50)'])} | ${fmt(v['p(90)'])} | ${fmt(v['p(99)'])} | ${fmt(v.max)} |`);
    }
  }
  const failed = data.metrics.http_req_failed ? data.metrics.http_req_failed.values.passes : 0;
  const total = data.metrics.http_reqs ? data.metrics.http_reqs.values.count : 0;
  const rps = data.metrics.http_reqs ? data.metrics.http_reqs.values.rate.toFixed(0) : '-';
  lines.push('', `total requests: ${total}, throughput: ${rps} req/s, unexpected failures: ${failed}`);
  const text = lines.join('\n') + '\n';
  return {
    stdout: text,
    [`/scripts/results/${LABEL}.md`]: text,
    [`/scripts/results/${LABEL}.json`]: JSON.stringify(data, null, 2),
  };
}
