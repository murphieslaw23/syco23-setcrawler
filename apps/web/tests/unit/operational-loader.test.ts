import { mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import { useOperationalLoad } from '../../composables/useOperationalLoad'

describe('useOperationalLoad', () => {
  it('executes exactly once when a navigated page mounts already ready', async () => {
    const ready = ref(true)
    const execute = vi.fn()
    const Harness = defineComponent({
      setup() {
        useOperationalLoad(ready, execute)
        return () => h('div')
      }
    })
    mount(Harness)
    await Promise.resolve()
    ready.value = true
    await Promise.resolve()
    expect(execute).toHaveBeenCalledTimes(1)
  })

  it('executes exactly once when readiness becomes available', async () => {
    const ready = ref(false)
    const execute = vi.fn()
    const Harness = defineComponent({
      setup() {
        useOperationalLoad(ready, execute)
        return () => h('div')
      }
    })
    mount(Harness)
    ready.value = true
    await Promise.resolve()
    ready.value = true
    await Promise.resolve()
    expect(execute).toHaveBeenCalledTimes(1)
  })
})
