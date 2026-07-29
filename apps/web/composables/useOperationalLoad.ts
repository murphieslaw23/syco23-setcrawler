import { onScopeDispose, ref, watch, type Ref } from 'vue'

export function useOperationalLoad(ready: Ref<boolean>, execute: () => Promise<unknown> | unknown) {
  const started = ref(false)
  const run = () => {
    if (!ready.value || started.value) return
    started.value = true
    void execute()
  }
  const stop = watch(ready, run, { immediate: true })
  onScopeDispose(stop)
  return {
    started,
    retry() {
      started.value = false
      run()
    }
  }
}
