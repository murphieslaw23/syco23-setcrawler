<script setup lang="ts">
import type { SetRecord } from '~/types'
import { formatDate, formatDuration } from '~/utils/format'

defineProps<{ set: SetRecord }>()
</script>

<template>
  <article class="set-row">
    <SetArtwork :index="set.artwork_index" :alt="`${set.title} artwork`" />
    <div class="set-row__identity">
      <h2>{{ set.title }}</h2>
      <p>{{ set.artist_names.join(' · ') }}</p>
      <p>{{ set.event_name || 'Event unresolved' }}<span v-if="set.city"> · {{ set.city }}</span></p>
      <small v-if="set.import_job_id || set.duplicate_of_id" class="set-row__context">{{ set.duplicate_of_id ? `duplicate of ${set.duplicate_of_id}` : `job ${set.import_job_id}` }}</small>
    </div>
    <div class="set-row__source"><SourceBadge :source="set.source" /></div>
    <div class="set-row__duration utility-data">{{ formatDuration(set.duration_seconds) }}</div>
    <div class="set-row__date utility-data">{{ formatDate(set.published_at) }}</div>
    <ScoreBar class="set-row__score" :score="set.set_score" />
    <div class="set-row__status status-label">{{ set.review_status }}</div>
    <NuxtLink class="icon-button set-row__open" :to="`/inbox/${set.id}`" :aria-label="`Review ${set.title}`">
      <AppIcon name="arrow" />
    </NuxtLink>
  </article>
</template>
