<script setup lang="ts">
import { demoJobs, demoProviderHealth, demoSets, demoStats } from '~/data/demo'
import type { ImportJob, ImportJobPage, ProviderHealthStatus, SetPage, Stats } from '~/types'
import { fixtureValue } from '~/utils/runtime'
import { formatDate, sourceLabel } from '~/utils/format'

const runtime = useSycoRuntime()
const { get } = useApi()
const { ready, canEdit } = useAuth()
const { send } = useApi()
const emptyStats: Stats = { total_sets: 0, by_source: { youtube: 0, soundcloud: 0, freeteknomusic: 0 }, by_status: { inbox: 0, reviewing: 0, accepted: 0, rejected: 0, published: 0 }, score_bands: { high: 0, review: 0, low: 0 }, queue: { queued: 0, processing: 0, failed: 0 } }
const emptyPage: SetPage = { items: [], total: 0, limit: 3, offset: 0 }
const emptyProviders: ProviderHealthStatus = { youtube: { configured: false, enabled: false, mode: 'unavailable' }, soundcloud: { configured: false, enabled: false, mode: 'unavailable' }, freeteknomusic: { configured: false, enabled: false, mode: 'unavailable' } }
const { data: stats, error: statsError, execute: loadStats } = await useAsyncData<Stats>('dashboard-stats', () => get('/stats', demoStats), { server: false, immediate: false, default: () => fixtureValue(runtime.runtimeMode, demoStats, emptyStats) })
const { data: recent, error: recentError, execute: loadRecent } = await useAsyncData<SetPage>('dashboard-recent', () => get('/sets?limit=3', { items: demoSets.slice(0, 3), total: 6, limit: 3, offset: 0 }), { server: false, immediate: false, default: () => fixtureValue(runtime.runtimeMode, { items: demoSets.slice(0, 3), total: 6, limit: 3, offset: 0 }, emptyPage) })
const { data: providers, error: providersError, execute: loadProviders } = await useAsyncData<ProviderHealthStatus>('provider-health', () => get('/providers', demoProviderHealth), { server: false, immediate: false, default: () => fixtureValue(runtime.runtimeMode, demoProviderHealth, emptyProviders) })
const { data: recentJobs, error: jobsError, execute: loadJobs } = await useAsyncData<ImportJobPage>('dashboard-jobs', () => get('/imports/queue?limit=5', { items: demoJobs, total: demoJobs.length, limit: 5, offset: 0 }), { server: false, immediate: false, default: () => ({ items: [], total: 0, limit: 5, offset: 0 }) })
useOperationalLoad(ready, loadStats)
useOperationalLoad(ready, loadRecent)
useOperationalLoad(ready, loadProviders)
useOperationalLoad(ready, loadJobs)

const providerWarnings = computed(() => {
  const warnings: string[] = []
  for (const [source, state] of Object.entries(providers.value || emptyProviders)) {
    if (!state.configured) warnings.push(`${sourceLabel(source as keyof ProviderHealthStatus)} is not configured.`)
    else if (!state.enabled) warnings.push(`${sourceLabel(source as keyof ProviderHealthStatus)} is paused.`)
  }
  for (const job of recentJobs.value?.items || []) {
    if (job.error_code?.includes('quota') || job.error_code?.includes('rate')) {
      warnings.push(`${sourceLabel(job.source)}: ${job.error_message || job.error_code}`)
    }
  }
  return [...new Set(warnings)]
})
const soundcloudBusy = ref(false)
const soundcloudError = ref('')
const soundcloudJobId = ref<string | null>(null)

async function importSoundCloud(url: string) {
  soundcloudError.value = ''
  soundcloudJobId.value = null
  if (!canEdit.value) {
    soundcloudError.value = 'Editor access is required to queue imports.'
    return
  }
  soundcloudBusy.value = true
  try {
    const parsed = new URL(url)
    if (parsed.protocol !== 'https:' || !['soundcloud.com', 'www.soundcloud.com'].includes(parsed.hostname)) {
      throw new Error('Use a valid SoundCloud track URL.')
    }
    const job = await send<ImportJob>('/imports/url', 'POST', { url })
    soundcloudJobId.value = job.id
    await loadJobs()
  } catch (error) {
    soundcloudError.value = error instanceof Error ? error.message : 'SoundCloud import could not be queued.'
  } finally {
    soundcloudBusy.value = false
  }
}
</script>

<template>
  <section>
    <PageHeader eyebrow="Editorial control" title="Overview" count="LIVESET DISCOVERY STATUS">
      <NuxtLink class="primary-button" to="/inbox">Open review inbox</NuxtLink>
    </PageHeader>

    <div class="stat-grid">
      <article>
        <span>Total sets</span>
        <strong>{{ stats?.total_sets || 0 }}</strong>
        <small>metadata records</small>
      </article>
      <article>
        <span>Review inbox</span>
        <strong>{{ stats?.by_status.inbox || 0 }}</strong>
        <small>human decision required</small>
      </article>
      <article>
        <span>High confidence</span>
        <strong>{{ stats?.score_bands.high || 0 }}</strong>
        <small>score ≥ 0.70</small>
      </article>
      <article>
        <span>Published</span>
        <strong>{{ stats?.by_status.published || 0 }}</strong>
        <small>publicly visible</small>
      </article>
    </div>

    <div class="dashboard-grid">
      <section class="plate">
        <div class="section-heading">
          <div>
            <p class="utility-label">Latest intake</p>
            <h2>Recent sets</h2>
          </div>
          <NuxtLink to="/sets">View all →</NuxtLink>
        </div>
        <div class="compact-set-list">
          <SetCard v-for="set in recent?.items || []" :key="set.id" :set="set" />
        </div>
      </section>
      <section class="plate source-breakdown">
        <p class="utility-label">Source balance</p>
        <h2>Provider intake</h2>
        <div v-for="(value, source) in stats?.by_source" :key="source">
          <span>{{ source }}</span>
          <strong>{{ value }}</strong>
          <i :style="{ width: `${(value / (stats?.total_sets || 1)) * 100}%` }" />
        </div>
      </section>
    </div>
    <section class="plate dashboard-providers">
      <div class="section-heading"><div><p class="utility-label">Provider capability</p><h2>Source health</h2></div><NuxtLink to="/imports">Open imports →</NuxtLink></div>
      <ProviderHealth :providers="providers || emptyProviders" />
      <p v-if="statsError || recentError || providersError || jobsError" class="form-message error" role="alert">Operational data could not be refreshed. {{ (statsError || recentError || providersError || jobsError)?.message }}</p>
    </section>
    <div class="dashboard-grid">
      <section class="plate">
        <div class="section-heading"><div><p class="utility-label">Durable queue</p><h2>Recent import runs</h2></div><NuxtLink to="/imports">View all →</NuxtLink></div>
        <article v-for="job in recentJobs?.items || []" :id="`job-${job.id}`" :key="job.id" class="job-row">
          <div class="job-row__main"><strong>{{ job.url || `Search profile ${job.profile_id || ''}` }}</strong><small>{{ sourceLabel(job.source) }} · {{ formatDate(job.created_at) }} · attempt {{ job.attempt_count }}</small></div>
          <JobStateBadge :status="job.status" />
          <NuxtLink v-if="job.result_set_id" class="secondary-button" :to="`/inbox/${job.result_set_id}`">Open set</NuxtLink>
        </article>
      </section>
      <section class="plate">
        <p class="utility-label">Provider warnings</p>
        <h2>Quota and availability</h2>
        <ul v-if="providerWarnings.length" class="warning-list"><li v-for="warning in providerWarnings" :key="warning">{{ warning }}</li></ul>
        <p v-else>No quota or rate-limit warnings.</p>
        <DashboardSoundCloudImport :can-edit="canEdit" :busy="soundcloudBusy" :error="soundcloudError" :job-id="soundcloudJobId" @submit="importSoundCloud" />
      </section>
    </div>
  </section>
</template>
