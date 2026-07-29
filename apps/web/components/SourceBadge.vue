<script setup lang="ts">
import type { SetSource } from '~/types'
import { sourceLabel } from '~/utils/format'

defineProps<{ source: SetSource }>()
</script>

<template>
  <span class="source-badge" :class="`source-badge--${source}`">{{ sourceLabel(source) }}</span>
</template>
