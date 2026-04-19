import LogConsole from '../components/LogConsole'

export default function LogsPage() {
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-bold text-gray-100">Logs</h1>
        <p className="text-sm text-gray-500">
          Watch scanner and backend activity in real time without leaving Leapfrog.
        </p>
      </div>
      <LogConsole />
    </div>
  )
}
