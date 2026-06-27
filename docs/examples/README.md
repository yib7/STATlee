# Sample data

`sample_survey.csv` is a fully synthetic social-science survey (320 fabricated
respondents, no real people). It has a built-in income/age effect on turnout, so
it gives an interpretable result for a first analysis such as:

> Does income predict voter turnout? Fit a logistic regression of `voted` on
> `income_k` and `age`, report the odds ratios, and plot the predicted
> probability of voting across income.

Columns: `respondent_id`, `age`, `income_k`, `education`, `region`,
`hours_social_media`, `party`, `voted` (0/1).
