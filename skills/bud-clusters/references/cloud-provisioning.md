# Provisioning a managed cloud cluster

The submit endpoint and the progress model are the same as an on-prem
onboarding - see `onboarding.md` for the form fields, the wizard mode and how
to read step states. This file covers only what differs.

## Sequence

Only `azure` (AKS) and `aws` (EKS) are implemented, even though the provider
list is much longer and every entry reports `is_enabled: true`. Match on
`unique_id`.

```bash
# 1. provider
bud api GET /credentials/cloud-providers \
  | jq -r '.providers[] | select(.unique_id=="azure") | "\(.id)  \(.schema_definition)"'

# 2. store a credential - Azure needs subscription_id, tenant_id, client_id,
#    client_secret; AWS needs access_key_id, secret_access_key
bud api POST /credentials/cloud-providers -d '{
  "provider_id": "<provider-id>",
  "credential_name": "eu-prod",
  "credential_values": {"subscription_id":"...","tenant_id":"...","client_id":"...","client_secret":"..."}
}'

# 3. the create response does NOT return the id - list and take the newest
bud api GET /credentials/cloud-providers/credentials -q provider_id=<provider-id> \
  | jq -r '.credentials[0].id'

# 4. region: use the id field, e.g. us-east-1 / westeurope
bud api GET /credentials/cloud-providers/<provider-id>/regions | jq -r '.regions[].id'

# 5. submit
scripts/cluster-onboard --type CLOUD --name prod-eu --icon "☁️" \
  --credential-id <cred-id> --provider-id <provider-id> --region westeurope \
  --wait --timeout 3600
```

`GET /credentials/cloud-providers/credentials/{id}` returns the single
credential **inside a list** (`credentials[0]`), and 403s if it belongs to
another user. Credential values come back masked.

Expect 10-25 minutes. Afterwards, read the cluster detail: `ingress_url` is
filled in from the provisioning output, and `cluster_type` is `CLOUD`. The
kubeconfig extraction path was written for AKS and is applied to every
provider, so **verify `ingress_url` after an EKS provision** before deploying
to it.

Keep the credential. Deleting a cloud cluster re-uses it to destroy the
infrastructure; if it was rotated or removed, deletion fails and the cluster
sticks in `deleting`.
