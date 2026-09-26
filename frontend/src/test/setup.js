import { config } from '@vue/test-utils'
import DsSelect from '../components/ui/DsSelect.vue'

// Application-wide components are registered here as well so component tests
// exercise the same rendered controls as the production app.
config.global.components = {
  ...config.global.components,
  DsSelect,
}
