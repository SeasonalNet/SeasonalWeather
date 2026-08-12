from seasonalweather.jobs.registry import policy_for


def accepted(job_type):
    return policy_for(job_type)
