import './styles/account-management.css'
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import './style.css'
import './styles/teacher-management.css'
import router from './router/index.js'
import App from './App.vue'
import DsSelect from './components/ui/DsSelect.vue'

const app = createApp(App)
app.component('DsSelect', DsSelect)

app.config.errorHandler = (err, instance, info) => {
  console.error('[Vue Error]', err, info)
}

app.use(createPinia())
app.use(router)
app.mount('#app')
