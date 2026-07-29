import { describe, expect, it } from 'vitest'

import { demoJobs } from '../../data/demo'
import { canRetry, createPollingController, filterJobs, hasActiveJobs } from '../../utils/jobs'

describe('job monitor helpers', () => {
  it('filters jobs by provider and state', () => {
    expect(filterJobs(demoJobs, { source: 'soundcloud', status: 'failed' })).toHaveLength(1)
  })

  it('only permits admins to retry terminal failures', () => {
    const deadLetter = demoJobs.find((job) => job.status === 'dead_letter')!
    expect(canRetry(deadLetter, 'admin')).toBe(true)
    expect(canRetry(demoJobs[0]!, 'editor')).toBe(false)
  })

  it('only schedules refreshes while a job remains active', () => {
    expect(hasActiveJobs(demoJobs)).toBe(true)
    expect(hasActiveJobs(demoJobs.filter((job) => ['failed', 'dead_letter'].includes(job.status)))).toBe(false)
  })

  it('cleans up polling as soon as no active job remains', () => {
    const calls: string[] = []
    const polling = createPollingController(
      () => calls.push('refresh'),
      (callback) => { calls.push('start'); return callback as unknown as number },
      () => calls.push('stop')
    )
    polling.sync(demoJobs)
    polling.sync(demoJobs.filter((job) => ['failed', 'dead_letter'].includes(job.status)))
    polling.stop()
    expect(calls).toEqual(['start', 'stop'])
  })
})
