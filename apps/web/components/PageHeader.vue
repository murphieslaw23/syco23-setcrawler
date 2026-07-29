<script setup lang="ts">
defineProps<{ title: string; eyebrow?: string; count?: string }>()
</script>

<template>
  <div class="page-header">
    <p v-if="eyebrow" class="utility-label">{{ eyebrow }}</p>
    <div class="page-header__line">
      <div>
        <h1>{{ title }}</h1>
        <p v-if="count" class="page-count">{{ count }}</p>
      </div>
      <slot />
    </div>
  </div>
</template>
