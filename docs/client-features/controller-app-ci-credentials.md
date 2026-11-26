# Controller App credentials for CI operations

Supported controller and CLI CI operations use the repository-bound controller
authentication provider at the actual GitHub request boundary. This includes
workflow and job observation, check observation, logs and artifacts, workflow
dispatch, the existing workflow-file repair, repair-admission reads, commit
history lookup, and explicit pending-deployment approval. Credential acquisition
or permission failure remains visible and does not fall back to a personal,
reviewer, or unauthenticated identity.

Each request can renew an expired installation token. CI observation and
same-process deployment-effect suppression use a stable, non-secret authority
namespace containing the repository, controller principal, and effective
permission scope, so renewal alone neither discards evidence nor repeats an
effect. Different repositories, principals, or permission scopes remain
isolated.

The controller App needs Actions read for workflow, job, log, and artifact
observation; Actions write for dispatch; Checks read for check observation;
Contents write and Workflows write for the existing definitive-422 workflow
repair; and Deployments write for pending-deployment review. Existing PR and
repository metadata reads additionally require their documented read grants.
GitHub environment reviewer rules still apply: an API grant does not confer
reviewer eligibility or bypass an environment rule.

GitHub API artifact requests are authenticated, but redirects to signed
foreign-origin download URLs strip GitHub authorization. Those downloads are
not GitHub quota observations, and credentials, signed URLs, and response
payloads are not diagnostic fields. This integration does not control traffic
inside Actions jobs, increase quota, add rerun/cancel behavior, or create a
separate credential-specific request governor.
