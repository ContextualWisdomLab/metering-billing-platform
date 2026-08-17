import { renderTenantApiCredential } from "../src/tenant_api_credential.js";
import activeOperatorKey from "../fixtures/active_operator_key.json";
import revokedLeakedKey from "../fixtures/revoked_leaked_key.json";

export default {
  title: "TenantApiCredential",
};

export const ActiveOperatorKey = {
  render: () => renderTenantApiCredential(activeOperatorKey),
};

export const RevokedLeakedKey = {
  render: () => renderTenantApiCredential(revokedLeakedKey),
};
