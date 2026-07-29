<script setup lang="ts">
import { demoProfiles } from '~/data/demo'
import type { SearchProfile } from '~/types'
import { fixtureValue } from '~/utils/runtime'
import { canManageProviders } from '~/utils/auth'
import { formatDate } from '~/utils/format'

const runtime = useSycoRuntime()
const { get, send } = useApi()
const { ready, role, isAdmin } = useAuth()
const { data: profiles, error, refresh, execute: loadProfiles } = await useAsyncData<SearchProfile[]>('profiles', () => get('/search-profiles', demoProfiles), {
  server: false,
  immediate: false,
  default: () => fixtureValue(runtime.runtimeMode, demoProfiles, [])
})
useOperationalLoad(ready, loadProfiles)
const name = ref('')
const query = ref('')
const schedule = ref('0 6 * * *')
const message = ref('')
const actionError = ref('')
const busyId = ref<string | null>(null)

function requireAdmin() {
  if (!canManageProviders(role.value)) throw new Error('Administrator access is required for provider profiles.')
}

async function addProfile() {
  actionError.value = ''
  message.value = ''
  try {
    requireAdmin()
    const created = await send<SearchProfile>('/search-profiles', 'POST', { name: name.value.trim(), query: query.value.trim(), schedule_cron: schedule.value, enabled: true })
    profiles.value = [...(profiles.value || []), created]
    name.value = ''
    query.value = ''
    message.value = 'Search profile created.'
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : 'Profile could not be created.'
  }
}

async function update(profile: SearchProfile, patch: Partial<Pick<SearchProfile, 'name' | 'query' | 'schedule_cron' | 'enabled'>>) {
  actionError.value = ''
  busyId.value = profile.id
  try {
    requireAdmin()
    const updated = await send<SearchProfile>(`/search-profiles/${profile.id}`, 'PATCH', patch)
    Object.assign(profile, updated)
    message.value = 'Search profile updated.'
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : 'Profile could not be updated.'
  } finally {
    busyId.value = null
  }
}

async function remove(profile: SearchProfile) {
  actionError.value = ''
  busyId.value = profile.id
  try {
    requireAdmin()
    await send(`/search-profiles/${profile.id}`, 'DELETE')
    profiles.value = (profiles.value || []).filter((item) => item.id !== profile.id)
    message.value = 'Search profile removed.'
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : 'Profile could not be removed.'
  } finally {
    busyId.value = null
  }
}

async function run(profile: SearchProfile) {
  actionError.value = ''
  busyId.value = profile.id
  try {
    requireAdmin()
    await send(`/search-profiles/${profile.id}/run`, 'POST')
    message.value = `${profile.name} queued for metadata search.`
    await refresh()
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : 'Profile could not be queued.'
  } finally {
    busyId.value = null
  }
}
</script>

<template>
  <section>
    <PageHeader title="Search Profiles" count="YOUTUBE QUOTA-CONTROLLED POLLING" />
    <p v-if="error" class="form-message error" role="alert">Profiles unavailable: {{ error.message }}</p>
    <p v-if="message" class="form-message" role="status">{{ message }}</p>
    <p v-if="actionError" class="form-message error" role="alert">{{ actionError }}</p>
    <div class="profile-layout">
      <section class="plate">
        <div class="section-heading"><div><p class="utility-label">Daily profiles</p><h2>Active queries</h2></div><span>{{ profiles?.filter((item) => item.enabled).length }} enabled</span></div>
        <article v-for="profile in profiles" :key="profile.id" class="profile-row">
          <span class="profile-state" :class="{ off: !profile.enabled }" />
          <div class="profile-row__fields"><label>Name<input v-model="profile.name" :disabled="!isAdmin" aria-label="Profile name"></label><label>Search query<input v-model="profile.query" :disabled="!isAdmin" aria-label="Profile search query"></label></div>
          <input v-model="profile.schedule_cron" class="profile-cron" :disabled="!isAdmin" aria-label="Schedule cron">
          <label class="profile-enabled"><input :checked="profile.enabled" type="checkbox" :disabled="!isAdmin || busyId === profile.id" @change="update(profile, { enabled: ($event.target as HTMLInputElement).checked })"> enabled</label>
          <dl class="profile-run-state">
            <div><dt>Last run</dt><dd>{{ profile.last_run_at ? formatDate(profile.last_run_at) : 'Never' }}</dd></div>
            <div><dt>Results</dt><dd>{{ profile.last_result_count ?? '—' }}</dd></div>
            <div><dt>Next page</dt><dd>{{ profile.next_page_token || 'Start' }}</dd></div>
          </dl>
          <p v-if="profile.last_error_code" class="form-message error" role="alert">Profile error: {{ profile.last_error_code }}</p>
          <NuxtLink v-if="profile.latest_job_id" class="text-button" :to="`/imports#job-${profile.latest_job_id}`">Latest job {{ profile.latest_job_id }}</NuxtLink>
          <div class="profile-row__actions">
            <button v-if="isAdmin" class="text-button" type="button" :disabled="busyId === profile.id" @click="update(profile, { name: profile.name, query: profile.query, schedule_cron: profile.schedule_cron })">Save</button>
            <button v-if="isAdmin" class="secondary-button" type="button" :disabled="busyId === profile.id" @click="run(profile)">Run now</button>
            <button v-if="isAdmin" class="danger-button" type="button" :disabled="busyId === profile.id" @click="remove(profile)">Delete</button>
          </div>
        </article>
      </section>
      <form v-if="isAdmin" class="plate create-profile" @submit.prevent="addProfile">
        <p class="utility-label">New profile</p><h2>Add YouTube query</h2>
        <label>Name<input v-model="name" required placeholder="Crew or genre profile"></label>
        <label>Search query<input v-model="query" required placeholder="freetekno liveset"></label>
        <label>Schedule<input v-model="schedule" required></label>
        <button class="primary-button" type="submit">Create profile</button>
      </form>
      <aside v-else class="plate access-note"><p class="utility-label">Read only</p><h2>Admin controls locked</h2><p>Provider configuration and manual runs are limited to administrators.</p></aside>
    </div>
  </section>
</template>
