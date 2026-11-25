# Post-Validation Routing Drain Admission

The ordinary Issue worker treats submitted-parent validation and the following
authoritative stage-routing pass as separate admission boundaries. If graceful
shutdown starts while validation is in flight, the worker finishes or joins that
owned validation but does not begin routing reads, writes, or dispatch afterward.

The claimed invalidation is released without acknowledgement at that boundary.
Its generation, any newer accepted invalidation, and durable validation evidence
therefore remain available for authoritative reevaluation after restart. While
the worker remains running, validation success and failure retain the existing
post-validation routing and error-precedence behavior.

This handoff changes no execution-trace event schema or dashboard projection:
the worker simply stops before entering the already-observed routing operation,
and its existing active-operation and durable-pending-work views remain
authoritative. The focused regressions in `tests/test_graceful_shutdown.py`
exercise successful and failed late validation, newer-generation recovery, and
the normal running control through the production worker boundary.
