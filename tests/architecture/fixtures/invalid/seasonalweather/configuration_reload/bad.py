import redis

from seasonalweather.worker.handlers import commit_configuration

authority = commit_configuration, redis
