# Contract provenance

Vendored from https://github.com/openmaxai/openmax-agent-sdk
- commit: 874dc0c407ec157c2878b30f3605ff0eed5cbb49
- package: @openmaxai/openmax-agent-sdk 0.1.0-alpha.0
- contents: schemas/v1 (JSON Schema 2020-12) + fixtures/v1 (golden conformance corpus)

Per that repo's CONTRACT.md, passing fixtures/v1 against schemas/v1 is the
definition of protocol conformance for any SDK in any language. Do not edit
these files here — re-vendor from upstream and note the new commit.

## Hermes runtime overlay

The vendored v1 corpus classifies `silent` as an admitted `handle:true` policy
decision. Hermes preserves that normalization result for conformance, but its
production bridge consumes the admitted message into bounded bridge-owned
history and advances watermarks before the host callback. It does not deliver
the message into a Hermes session/model turn. This intentional runtime overlay
implements bridge-only observation without modifying the vendored contract;
changing the cross-runtime schema or fixtures requires an upstream SDK change
and a later re-vendor.
