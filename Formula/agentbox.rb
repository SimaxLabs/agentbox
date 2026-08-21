class Agentbox < Formula
  desc "Back up and restore AI coding-agent resources"
  homepage "https://github.com/SimaxLabs/agentbox"
  version "0.1.3"
  license "GPL-3.0-only"

  on_macos do
    depends_on arch: :arm64
    url "https://github.com/SimaxLabs/agentbox/releases/download/v0.1.3/agentbox-0.1.3-macos-arm64.tar.gz"
    sha256 "f80db9b8c757f7339f66feb03cf76307614834cccb1e8398a8373e3983fb37eb"
  end

  on_linux do
    on_intel do
      depends_on arch: :x86_64
      url "https://github.com/SimaxLabs/agentbox/releases/download/v0.1.3/agentbox-0.1.3-linux-x86_64.tar.gz"
      sha256 "91e73c8574de4b40900361294d4e04cbfde36f6ef61bf434a60726e05619c357"
    end

    on_arm do
      depends_on arch: :arm64
      url "https://github.com/SimaxLabs/agentbox/releases/download/v0.1.3/agentbox-0.1.3-linux-arm64.tar.gz"
      sha256 "67fab0c1ccbb16523d6f273b1629de821e318d4d37ba1c53548471f490eefeff"
    end
  end

  def install
    libexec.install "agentbox"
    (bin/"agentbox").write_env_script libexec/"agentbox",
                                       AGENTBOX_INSTALL_CHANNEL: "homebrew"
  end

  test do
    assert_match "AgentBox v0.1.3", shell_output("#{bin}/agentbox --version")
  end
end
