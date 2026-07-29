import type { AppRole } from './auth'
import type { ImportJob, ImportJobStatus, SetSource } from '~/types'

export interface JobFilters {
  source: 'all' | SetSource
  status: 'all' | ImportJobStatus
}

export function filterJobs(jobs: ImportJob[], filters: JobFilters) {
  return jobs.filter((job) => (
    (filters.source === 'all' || job.source === filters.source)
    && (filters.status === 'all' || job.status === filters.status)
  ))
}

export function canRetry(job: ImportJob, role: AppRole) {
  return role === 'admin' && (job.status === 'failed' || job.status === 'dead_letter')
}

export function hasActiveJobs(jobs: ImportJob[]) {
  return jobs.some((job) => ['queued', 'processing', 'retry'].includes(job.status))
}

type StartInterval = (callback: () => void, delay: number) => unknown
type StopInterval = (timer: unknown) => void

export function createPollingController(
  refresh: () => void,
  startInterval: StartInterval = setInterval,
  stopInterval: StopInterval = (timer) => clearInterval(timer as ReturnType<typeof setInterval>)
) {
  let timer: unknown | undefined
  const stop = () => {
    if (timer !== undefined) {
      stopInterval(timer)
      timer = undefined
    }
  }
  return {
    sync(jobs: ImportJob[]) {
      if (hasActiveJobs(jobs) && timer === undefined) timer = startInterval(refresh, 5_000)
      if (!hasActiveJobs(jobs)) stop()
    },
    stop
  }
}
