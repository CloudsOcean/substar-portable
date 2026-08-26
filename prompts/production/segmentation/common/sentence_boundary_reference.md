# Native sentence boundary policy: soft reference

`sentence_start` and `sentence_end` are native ASR hypotheses. Treat them as useful but fallible reference evidence, never as mandatory Cue or meaning-group boundaries.

- Preserve a native boundary when syntax, discourse function, timing or speaker evidence supports it.
- Move, cross or remove it when adjacent words form one unfinished clause, paired construction, required complement, quotation or other indivisible semantic structure.
- Build meaning groups from the continuous word-level timeline; do not merely subdivide each native sentence independently.
- Punctuation near a native boundary is also an ASR hypothesis and must not override clear lexical or syntactic continuity.
