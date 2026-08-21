class Agentbox < Formula
  desc "Back up and restore AI coding-agent resources"
  homepage "https://github.com/SimaxLabs/agentbox"
  version "0.1.2"
  license "GPL-3.0-only"

  on_macos do
    depends_on arch: :arm64
    url "https://github.com/SimaxLabs/agentbox/releases/download/v0.1.2/agentbox-0.1.2-macos-arm64.tar.gz"
    sha256 "fd2cf862516935b8106d74f25f49d32a86d52801427278200312803696cf96cf"
  end

  on_linux do
    on_intel do
      depends_on arch: :x86_64
      url "https://github.com/SimaxLabs/agentbox/releases/download/v0.1.2/agentbox-0.1.2-linux-x86_64.tar.gz"
      sha256 "69715b14d754dd27e1b69b96ac6adb28f5cb44bcc5fddc28bd4cf698829905df"
    end

    on_arm do
      depends_on arch: :arm64
      url "https://github.com/SimaxLabs/agentbox/releases/download/v0.1.2/agentbox-0.1.2-linux-arm64.tar.gz"
      sha256 "457fa6f7ce9b93a8a3d882533bc09dfde97ffd379010abdc3a4ed272db928ec0"
    end
  end

  def install
    libexec.install "agentbox"
    (bin/"agentbox").write_env_script libexec/"agentbox",
                                       AGENTBOX_INSTALL_CHANNEL: "homebrew"
  end

  test do
    assert_match "AgentBox v0.1.2", shell_output("#{bin}/agentbox --version")
  end
end
