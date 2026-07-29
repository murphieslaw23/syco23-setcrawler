<script setup lang="ts">
import { formatScore } from '~/utils/format'

defineProps<{ score: number; compact?: boolean }>()
</script>

<template>
  <div class="score" :class="{ 'score--compact': compact }" :aria-label="`Confidence ${formatScore(score)}`">
    <span class="score__value">{{ formatScore(score) }}</span>
    <span class="score__track"><i :style="{ width: `${Math.round(score * 100)}%` }" /></span>
  </div>
</template>
