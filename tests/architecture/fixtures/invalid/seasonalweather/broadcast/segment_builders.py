class SegmentCandidate:
    def __post_init__(self):
        self.text = self.text.strip()[:12000]
