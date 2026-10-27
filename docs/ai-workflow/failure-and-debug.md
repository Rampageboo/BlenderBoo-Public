# Failure Resolution

Read this module for repeated failure or blocked validation.

Read the exact error, inspect the relevant path, make a bounded repair, and
rerun the failing check. If the same approach is not producing new evidence,
change approach, commission research under `research-integration.md`, or stop
with the evidence and unresolved decision. Escalate earlier when the fix requires a product,
architecture, security, migration, or data-risk decision.

If validation cannot run, report the command, blocker, reasoning performed, and
remaining uncertainty. External research may help with missing knowledge but
does not replace reproduction or validation.

## Cycles and improvement

One repair followed by a meaningful rerun against the same hypothesis is one
cycle. A cycle improved when an acceptance-relevant result moved toward its
threshold — a required failing test passes, a reproduced defect stops
reproducing, a failure count falls. A different error or a changed test count is
new evidence, which may justify a new hypothesis but is not improvement.

After two cycles without improvement, stop changing code on that hypothesis.
State the evidence for and against it, and make the next action an experiment
that distinguishes the credible alternatives rather than another variation of
the same repair.

After a third failed cycle, or when no bounded discriminating experiment exists,
stop and obtain review outside the frame that produced the approach. Rerunning
the same checks does not satisfy this stop.

When a premise turns out to be false, withdraw what rested on it rather than
repairing around it. Stop the work that depended on it, say which conclusions
still have independent support and which are now unestablished, and re-derive
from the earliest affected decision. The question is not whether the original
conclusion can be salvaged — it is whether it stands without the premise.

## Qualifying evidence

Outcome evidence shows that a test passes, a symptom stopped, or an output was
produced. It does not by itself prove why. Causal evidence supports a mechanism;
measurement evidence supports a number under stated conditions. Label the
claim at the level the evidence actually supports.

Before making a material root-cause claim that will enter durable documents,
drive structural change, reject another credible solution, guide recurring
incidents, or rests mainly on "the symptom disappeared after this change":

- state what the candidate explanation predicts;
- name a result that would falsify it;
- check at least one credible competing explanation; and
- say whether the evidence establishes symptom removal or the mechanism.

For performance, time, memory, VRAM, or throughput claims, record the build
mode, machine or material hardware, concurrent load, cold/warm state, sample or
repeat count, and statistic used. An uncontrolled measurement is an
observation, not a regression claim.
