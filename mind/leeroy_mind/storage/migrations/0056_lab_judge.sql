-- The lab judge (lab/judge.py): its 1-5 score of one answer ("would a real person have texted
-- this", measured against a strong grader by `lab judge-eval`) and the model calls it took.
-- NULL: not judged this way (the 0-3 rubric, or not graded yet). Numbers only.
ALTER TABLE lab_score ADD COLUMN score REAL;
ALTER TABLE lab_score ADD COLUMN score_calls INTEGER;
