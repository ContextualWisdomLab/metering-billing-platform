import { renderAccountStatement } from "../src/account_statement.js";
import settledAccountStatement from "../fixtures/settled_account_statement.json";
import voidedAccountStatement from "../fixtures/voided_account_statement.json";

export default {
  title: "AccountStatement",
};

export const Settled = {
  render: () => renderAccountStatement(settledAccountStatement),
};

export const InclusiveVoids = {
  render: () => renderAccountStatement(voidedAccountStatement),
};
