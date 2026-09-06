# API stability and compatibility

The supported Python import surface is the set of names exported by `__all__`
from these modules:

- `stanag4609`
- `stanag4609.audio`
- `stanag4609.klv`
- `stanag4609.player`
- `stanag4609.sidecar`
- `stanag4609.transport`

CI checks that every exported name exists, that no export is duplicated, and
that the reviewed surface has not changed accidentally. Imports from deeper
implementation modules are private unless a guide explicitly identifies them
as an extension point.

## Compatibility policy

Patch releases preserve the documented public API and wire behavior. During the
pre-1.0 series, a minor release may make an incompatible change only when the
changelog identifies it, an upgrade note explains it, and a deprecation cycle
is used whenever the old behavior can be retained safely. Removing a public
name therefore requires an intentional update to the API baseline and review of
all documentation and examples.

The following are not breaking API changes:

- adding a new export, optional adapter, validation finding, or enum member;
- accepting additional standards-valid input;
- rejecting input that violates a documented bound or normative requirement;
- changing undocumented internals; or
- changing the reference UI without changing its documented HTTP contracts.

Applications should not compare complete diagnostic prose. Use typed exception
classes, verifier finding codes, report schema versions, and structured fields.
Unknown KLV items remain losslessly preservable unless a caller explicitly
selects strict rejection or transformation.

The project follows semantic versioning after `1.0`. Until then, the version and
Alpha classifier communicate that the API can still evolve under the guarded
minor-release policy above. This is independent of the production-readiness of
the documented protocol profiles.

