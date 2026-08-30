BEGIN;

CREATE OR REPLACE FUNCTION billing_core.require_new_issued_invoice_contract_v2()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.issued_invoice_contract_version <> 2 THEN
        RAISE EXCEPTION 'new issued invoice contract version must be 2';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER issued_invoice_contract_version_v2_validate
BEFORE INSERT ON billing_core.issued_invoice
FOR EACH ROW EXECUTE FUNCTION billing_core.require_new_issued_invoice_contract_v2();

COMMIT;
