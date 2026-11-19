-- The lab judge (lab/judge.py): its 1-5 score of one answer ("would a real person have texted
-- this", measured against a strong grader by `lab judge-eval`) and the model calls it took.
-- NULL: not judged this way (the 0-3 rubric, or not graded yet). `prefer`, on a variant's row: its
-- answer's share against the control's answer to the same line, asked both ways round (1 it read
-- more like a real person both times, 0 the control's did, 0.5 a tie or a split). Numbers only.
ALTER TABLE lab_score ADD COLUMN score REAL;
ALTER TABLE lab_score ADD COLUMN score_calls INTEGER;
ALTER TABLE lab_score ADD COLUMN prefer REAL;
