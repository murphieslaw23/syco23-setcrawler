<script setup lang="ts">
const props = defineProps<{ index?: number; alt: string }>()
const position = computed(() => `${(Math.min(3, props.index || 0) / 3) * 100}% center`)
</script>

<template>
  <div
    class="set-artwork"
    role="img"
    :aria-label="alt"
    :style="{ backgroundPosition: position }"
  />
</template>
