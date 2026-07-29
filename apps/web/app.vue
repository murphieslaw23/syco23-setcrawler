<script setup lang="ts">
import { demoStats } from '~/data/demo'
import { mobileNavigation, navigation } from '~/data/navigation'
import type { Stats } from '~/types'
import { fixtureValue } from '~/utils/runtime'

const route = useRoute()
const router = useRouter()
const config = useRuntimeConfig()
const { get } = useApi()
const { user, role, ready, signOut } = useAuth()
const globalSearch = ref('')
const identityBusy = ref(false)
const identityError = ref('')
const emptyStats: Stats = { total_sets: 0, by_source: { youtube: 0, soundcloud: 0, freeteknomusic: 0 }, by_status: { inbox: 0, reviewing: 0, accepted: 0, rejected: 0, published: 0 }, score_bands: { high: 0, review: 0, low: 0 }, queue: { queued: 0, processing: 0, failed: 0 } }
const { data: stats, execute: loadStats } = await useAsyncData<Stats>('global-stats', () => get('/stats', demoStats), {
  server: false,
  immediate: false,
  default: () => fixtureValue(config.public.runtimeMode as string, demoStats, emptyStats)
})
useOperationalLoad(ready, loadStats)

function isActive(to: string) {
  return to === '/' ? route.path === '/' : route.path.startsWith(to)
}

function submitSearch() {
  const value = globalSearch.value.trim()
  router.push(value ? { path: '/sets', query: { search: value } } : '/sets')
}

async function leave() {
  identityBusy.value = true
  identityError.value = ''
  try {
    await signOut()
    await router.push('/login')
  } catch (error) {
    identityError.value = error instanceof Error ? error.message : 'Could not sign out.'
  } finally {
    identityBusy.value = false
  }
}
</script>

<template>
  <div class="app-frame">
    <header class="masthead">
      <NuxtLink class="brand" to="/" aria-label="SYCO23 Setcrawler overview">
        <span class="brand__mark">23</span>
        <span>
          <small>SYSTEM CORRUPT</small>
          <strong>SYCO23 SETCRAWLER</strong>
        </span>
      </NuxtLink>
      <form class="global-search" role="search" @submit.prevent="submitSearch">
        <AppIcon name="search" />
        <input v-model="globalSearch" type="search" placeholder="Search sets, artists, events…" aria-label="Global search">
      </form>
      <NuxtLink class="primary-button masthead__import" to="/import">
        <AppIcon name="import" />
        Import URL
      </NuxtLink>
      <div class="identity-control">
        <NuxtLink v-if="!user" class="role-button" to="/login">{{ role }} <span>sign in</span></NuxtLink>
        <button v-else class="role-button" type="button" :disabled="identityBusy" @click="leave">{{ role }} <span>sign out</span></button>
        <p v-if="identityError" role="alert">{{ identityError }}</p>
      </div>
    </header>

    <div class="app-body">
      <aside class="side-nav">
        <nav aria-label="Primary navigation">
          <NuxtLink
            v-for="item in navigation"
            :key="item.to"
            :to="item.to"
            :class="{ active: isActive(item.to) }"
          >
            <AppIcon :name="item.icon" :size="22" />
            <span>{{ item.label }}</span>
            <i />
          </NuxtLink>
        </nav>
        <div class="side-signature">
          <div class="totem-mark" aria-hidden="true"><i /><i /><i /></div>
          <p>SYCO23 // SYSTEM CORRUPT</p>
          <span>Freetekno liveset curation</span>
          <b>SC23</b>
        </div>
      </aside>

      <main id="main-content" class="main-surface">
        <NuxtPage />
      </main>

      <QueueRail :queue="stats?.queue || emptyStats.queue" />
    </div>

    <nav class="mobile-nav" aria-label="Mobile navigation">
      <NuxtLink
        v-for="item in mobileNavigation"
        :key="item.to"
        :to="item.to"
        :class="{ active: isActive(item.to) }"
      >
        <AppIcon :name="item.icon" :size="21" />
        <span>{{ item.label }}</span>
      </NuxtLink>
    </nav>
  </div>
</template>
