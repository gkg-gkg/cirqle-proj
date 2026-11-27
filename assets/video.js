// Demo videos: show the poster + play button until clicked, then hand over
// to the browser's own controls. When the video ends it resets to the poster.
document.querySelectorAll('.vid').forEach(function (fig) {
  var video = fig.querySelector('video');
  var play = fig.querySelector('.vid-play');
  if (!video || !play) return;

  play.addEventListener('click', function () {
    fig.classList.add('is-playing');
    video.controls = true;
    video.play();
    video.focus();
  });

  video.addEventListener('ended', function () {
    fig.classList.remove('is-playing');
    video.controls = false;
    video.load(); // back to the poster
    play.focus();
  });
});
