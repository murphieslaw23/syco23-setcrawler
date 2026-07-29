<script setup lang="ts">
import type { ProviderHealthStatus } from '~/types'
import { sourceLabel } from '~/utils/format'

defineProps<{ providers: ProviderHealthStatus }>()
</script>

<template>
  <section class="provider-health" aria-label="Provider health">
    <article v-for="(provider, source) in providers" :key="source" class="provider-health__item">
      <span class="provider-health__mark" :class="{ muted: !provider.enabled }" aria-hidden="true" />
      <div>
        <strong>{{ sourceLabel(source) }}</strong>
        <small>{{ provider.mode.replaceAll('_', ' ') }}</small>
      </div>
      <span class="provider-health__state">{{ provider.enabled ? 'ready' : provider.configured ? 'paused' : 'not configured' }}</span>
    </article>
  </section>
</template>
