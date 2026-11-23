# Issue #401 root-cause analysis

The repair is limited to the source-specific prompt and validation boundaries selected by the exact BookMarks source name and absolute category URL.
The shared extraction validators remain strict, and invalid model output is not filled, normalized, salvaged, or converted into entries.
1. Issue #363 shows that BookMarks entries were rejected before defaults because each entry lacked one or more required Source Mention fields.
The retained evidence does not preserve the raw invalid output or an exact per-field aggregate, so this analysis does not claim a particular missing field.
The generic prompt did not bind the BookMarks canonical signal type, signal strength, candidate category, or a final all-fields checklist.
2. The rejected output used URL values that could not be verified against the fixed capture.
The generic prompt allowed the model to choose item URLs from large repetitive HTML instead of binding BookMarks to the known configured category URL.
3. The production adapter has an additional absolute HTTP(S) URL guard after the earlier provenance lookup.
A relative URL can occur in the capture and pass the earlier provenance lookup before the production adapter rejects it.
The missing explicit absolute-URL binding in the generic prompt is the source-specific prompt boundary for this failure.
4. The fixed capture contains BookMarks latest_book listing rows, so a bound empty entries array is not an authoritative no-result artifact.
The same deterministic listing-presence check now rejects that empty artifact in both production validation and replay's authoritative-empty path.
5. The first authorized post-prompt probe produced 3 non-empty validated outputs and 1 empty output, which exposed this validation gap.
The empty output is preserved as the distinct unexpected_empty_extraction invalid class; it is not counted as repaired or validated.
This evidence records the gap and the deterministic repair boundary, not a final production-repair claim.
6. The preserved current artifact is bound to an older operation and capture digest, so its end-to-end result remains an identity mismatch.
The repair does not rewrite or reclassify that preserved artifact.
7. Issue #363 recorded invalid outcomes in intrinsic and production-shaped workers 1, 2, and 3, so worker count is not causal evidence for this repair.
One observation for each mode is not enough to make a timeout or worker-policy decision.
The offline repair evidence does not justify a timeout or worker policy change.
A separate timeout and worker-policy investigation remains needed only if later production-faithful evidence still shows slow or censored outcomes.
