# SK-01 Structured Runtime Envelope

The runtime sends the locked SK-01 entry file and both required reference files unchanged, together with the persisted invocation context. It asks the same model invocation to return only this strict object:

```json
{
  "student_visible_text": "one student-visible Socratic question or boundary notice",
  "teaching_status": "continue",
  "teaching_progress": {"checkpoint": "clarifying"}
}
```

`teaching_status` is one of `continue`, `natural_end`, or `boundary_stop`. `checkpoint` is one of `clarifying`, `examining`, `error_cause`, `prevention_rule`, `transfer_checkpoint`, `finished`, or `boundary`.

The envelope does not add a teaching sequence, solve the task, transform the student-visible wording, or infer completion. A missing, invalid, refused, incomplete, or unparsable response is an error, not a completion signal.
