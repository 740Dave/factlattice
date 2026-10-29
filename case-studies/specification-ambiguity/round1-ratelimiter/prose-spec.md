# Feature request: per-user rate limiting

We need a rate limiter for our API. Each user should be able to make up to 5
requests every 10 seconds. If a user goes over that, reject the extra requests
until they're back under the limit.

Please implement it and include some tests.
