from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

DEFAULT_APP_ID = "SteamShotBridge_SingleInstance_App"


class SingleInstance(QObject):
    show_requested = Signal()

    def __init__(
        self,
        app_id: str = DEFAULT_APP_ID,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.app_id = app_id
        self._server: QLocalServer | None = None

    def is_already_running(self) -> bool:
        socket = QLocalSocket(self)
        socket.connectToServer(self.app_id)
        if socket.waitForConnected(300):
            socket.write(b"mostrar\n")
            socket.flush()
            socket.waitForBytesWritten(300)
            socket.waitForReadyRead(100)
            socket.disconnectFromServer()
            return True

        QLocalServer.removeServer(self.app_id)
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._server.listen(self.app_id)
        return False

    def _on_new_connection(self) -> None:
        if self._server is None:
            return
        socket = self._server.nextPendingConnection()
        if socket is not None:
            if socket.bytesAvailable() > 0:
                self._read_socket(socket)
            else:
                socket.readyRead.connect(lambda: self._read_socket(socket))

    def _read_socket(self, socket: QLocalSocket) -> None:
        data = bytes(socket.readAll()).decode("utf-8", errors="ignore")
        if "mostrar" in data:
            self.show_requested.emit()
            try:
                socket.write(b"ok\n")
                socket.flush()
            except Exception:
                pass
        socket.disconnectFromServer()

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            QLocalServer.removeServer(self.app_id)
            self._server = None
