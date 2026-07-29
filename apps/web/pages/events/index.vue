<script setup lang="ts">
import { demoSets } from '~/data/demo'
import { fixtureValue } from '~/utils/runtime'
const runtime = useSycoRuntime()
const events = fixtureValue(runtime.runtimeMode, demoSets, []).filter((set) => set.event_name).map((set) => ({
  id: set.id,
  name: set.event_name,
  city: set.city,
  year: set.year || new Date(set.published_at).getFullYear(),
  artwork: set.artwork_index
}))
</script>

<template>
  <section>
    <PageHeader title="Events" :count="`${events.length} LINKED GATHERINGS`" />
    <div class="directory-grid">
      <article v-for="event in events" :key="event.id" class="directory-card">
        <SetArtwork :index="event.artwork" :alt="`${event.name} visual`" />
        <div><span class="utility-label">{{ event.city || 'Location unresolved' }} · {{ event.year }}</span><h2>{{ event.name }}</h2><p>1 linked set</p></div>
        <AppIcon name="arrow" />
      </article>
    </div>
    <p v-if="!events.length" class="empty-state">Event records are not available in this runtime.</p>
  </section>
</template>
