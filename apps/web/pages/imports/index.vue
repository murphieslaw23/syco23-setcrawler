<script setup lang="ts">
import { demoJobs } from '~/data/demo'
import type { ImportJobPage, ImportJobStatus, SetSource } from '~/types'
import { canRetry, createPollingController, filterJobs } from '~/utils/jobs'
import { formatDate, sourceLabel } from '~/utils/format'

const config = useRuntimeConfig()
const { get, send } = useApi()
const { role, ready, isAdmin, canEdit } = useAuth()
const source = ref<'all' | SetSource>('all')
const statusFilter = ref<'all' | ImportJobStatus>('all')
const message = ref('')
const actionError = ref('')

const { data: page, error, status, refresh, execute: loadQueue } = await useAsyncData<ImportJobPage>(
  'imports-queue',
  () => get('/imports/queue?limit=50', { items: demoJobs, total: demoJobs.length, limit: 50, offset: 0 }),
  { server: false, immediate: false, default: () => ({ items: [], total: 0, limit: 50, offset: 0 }) }
)
useOperationalLoad(ready, loadQueue)

const jobs = computed(() => filterJobs(page.value?.items || [], { source: source.value, status: statusFilter.value }))
const polling = createPollingController(() => { void refresh() })
watch(() => page.value?.items, (items) => polling.sync(items || []), { deep: true })
onMounted(() => polling.sync(page.value?.items || []))
onBeforeUnmount(polling.stop)

async function retry(jobId: string) {
  actionError.value = ''
  message.value = ''
  if (!isAdmin.value) {
    actionError.value = 'Administrator access is required to retry jobs.'
    return
  }
  try {
    await send(`/imports/queue/${jobId}/retry`, 'POST')
    message.value = 'Retry job queued.'
    await refresh()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : 'Retry could not be queued.'
  }
}
</script>

<template>
  <section>
    <PageHeader title="Imports" :count="`${page?.total || 0} JOBS IN QUEUE`">
      <NuxtLink v-if="canEdit" class="primary-button" to="/import">Queue URL</NuxtLink>
    </PageHeader>
    <div class="filter-bar import-filters">
      <label>Provider
        <select v-model="source"><option value="all">All providers</option><option value="youtube">YouTube</option><option value="soundcloud">SoundCloud</option><option value="freeteknomusic">FreeTeknoMusic</option></select>
      </label>
      <label>State
        <select v-model="statusFilter"><option value="all">All states</option><option value="queued">Queued</option><option value="processing">Processing</option><option value="retry">Retry</option><option value="completed">Completed</option><option value="failed">Failed</option><option value="blocked">Blocked</option><option value="dead_letter">Dead letter</option></select>
      </label>
      <button class="text-button" type="button" @click="source = 'all'; statusFilter = 'all'">Clear ×</button>
    </div>
    <p v-if="error" class="form-message error" role="alert">Queue unavailable: {{ error.message }}</p>
    <p v-if="message" class="form-message" role="status">{{ message }}</p>
    <p v-if="actionError" class="form-message error" role="alert">{{ actionError }}</p>
    <div class="job-list" :aria-busy="status === 'pending'">
      <article v-for="job in jobs" :id="`job-${job.id}`" :key="job.id" class="job-row">
        <div class="job-row__source"><SourceBadge :source="job.source" /><small>{{ job.job_type.replace('_', ' ') }}</small></div>
        <div class="job-row__main"><strong>{{ job.url || `Search profile ${job.profile_id || ''}` }}</strong><small>{{ sourceLabel(job.source) }} · created {{ formatDate(job.created_at) }} · attempt {{ job.attempt_count }}</small><p v-if="job.error_message">{{ job.error_message }}</p></div>
        <JobStateBadge :status="job.status" />
        <NuxtLink v-if="job.result_set_id" class="secondary-button" :to="`/inbox/${job.result_set_id}`">Open set</NuxtLink>
        <button v-else-if="canRetry(job, role)" class="secondary-button" type="button" @click="retry(job.id)">Retry</button>
      </article>
      <div v-if="!jobs.length" class="empty-state">No import jobs match the current filters.</div>
    </div>
  </section>
</template>
