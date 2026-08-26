// End-to-end smoke and baseline load test for the compose deployment.
//
// Run it against a healthy stack:
//   k6 run compose/k6/e2e_smoke.js
//
// The scenario ramps to a fixed peak of 50 virtual users over 60 seconds,
// then sustains that peak for another 60 seconds.  Checks stay sanity-level
// (status correctness only); no performance thresholds are enforced here so
// the recorded baseline numbers stay comparable run over run.
import http from 'k6/http';
import { check } from 'k6';
import { Trend } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const TENANT_REFERENCE = __ENV.TENANT_REFERENCE || 'urn:cwl:k6_tenant_001';

const healthzDuration = new Trend('e2e_healthz_duration');
const readyzDuration = new Trend('e2e_readyz_duration');
const tenantReadDuration = new Trend('e2e_tenant_read_duration');

export const options = {
  scenarios: {
    e2e_smoke: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '60s', target: 50 },
        { duration: '60s', target: 50 },
      ],
      gracefulRampDown: '10s',
    },
  },
};

export function setup() {
  // Bootstrap one tenant API credential over HTTP.  Before this first issue
  // the tenant pin alone is accepted (bootstrap window), so no secret is
  // needed in the environment.
  const issueResponse = http.post(
    `${BASE_URL}/v1/tenant-api-credentials`,
    JSON.stringify({ credential_label: 'k6_baseline_runner' }),
    {
      headers: {
        'Content-Type': 'application/json',
        'X-CWL-Tenant-Reference': TENANT_REFERENCE,
      },
      tags: { name: 'setup_issue_credential' },
    },
  );
  const issued = check(issueResponse, {
    'setup credential issue is 200': (response) => response.status === 200,
  });
  if (!issued || issueResponse.json('api_credential_secret') === undefined) {
    throw new Error(`could not issue the k6 runner credential: ${issueResponse.status} ${issueResponse.body}`);
  }
  return { apiCredentialSecret: issueResponse.json('api_credential_secret') };
}

export default function (data) {
  // GET /healthz keeps a low weight: roughly one iteration in twenty.
  if ((__VU + __ITER) % 20 === 0) {
    const healthzResponse = http.get(`${BASE_URL}/healthz`, {
      tags: { name: 'get /healthz' },
    });
    healthzDuration.add(healthzResponse.timings.duration);
    check(healthzResponse, {
      'healthz is 200': (response) => response.status === 200,
    });
  }

  // GET /readyz must report the durable backend ready on every iteration.
  const readyzResponse = http.get(`${BASE_URL}/readyz`, {
    tags: { name: 'get /readyz' },
  });
  readyzDuration.add(readyzResponse.timings.duration);
  check(readyzResponse, {
    'readyz is 200': (response) => response.status === 200,
  });

  // Authenticated-style tenant read against the seeded tenant.
  const tenantReadResponse = http.get(`${BASE_URL}/v1/tenant-api-credentials`, {
    headers: {
      'X-CWL-Tenant-Reference': TENANT_REFERENCE,
      'X-CWL-Api-Key': data.apiCredentialSecret,
    },
    tags: { name: 'get /v1/tenant-api-credentials' },
  });
  tenantReadDuration.add(tenantReadResponse.timings.duration);
  check(tenantReadResponse, {
    'tenant read is 200': (response) => response.status === 200,
  });
}
