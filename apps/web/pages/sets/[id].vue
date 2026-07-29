<script setup lang="ts">
const route = useRoute()
await navigateTo(`/inbox/${route.params.id}`, { replace: true })
</script>

<template><span /></template>
